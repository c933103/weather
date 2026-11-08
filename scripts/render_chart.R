#!/usr/bin/env Rscript
# East Asia weather chart — ggplot2 drawing of chart_data.json.
#
# All data decisions (extrema windows, icons, grades, cascade note) are made
# in Python (scripts/chart_data.py, unit-tested) and arrive in the JSON this
# script reads. What happens here is only drawing, mirroring the retired
# matplotlib renderer: a solid line through the day highs and a dashed line
# through the noon-to-noon night lows per city, the high/low envelope as a
# translucent band behind them (each boundary curve held flat across the
# half-slot at a run's edges), monotone cubic (Fritsch-Carlson, the R
# counterpart of PCHIP) smoothing between the verified extrema, 1 °C minor
# y gridlines, date labels, and below the axis one row per city with the
# day's colour weather emoji (committed PNG assets extracted from Noto
# Color Emoji) and its confidence grade. Prints one JSON line reporting
# how many emoji it actually drew.
suppressPackageStartupMessages({
  library(ggplot2)
  library(jsonlite)
  library(grid)
  library(png)
  library(ragg)
  library(svglite)
})

argv <- commandArgs(trailingOnly = TRUE)
opt <- list()
for (i in seq(1, length(argv), by = 2)) opt[[sub('^--', '', argv[i])]] <- argv[i + 1]
input_path <- opt$input
output_path <- opt$output
assets_dir <- opt$assets

d <- fromJSON(input_path, simplifyVector = FALSE)
num <- function(x) as.numeric(x)
pair_list <- function(xs) lapply(xs, function(p) c(num(p[[1]]), num(p[[2]])))

cities <- lapply(d$cities, function(c) list(
  key = c$key, label = c$label,
  highs = pair_list(c$highs), lows = pair_list(c$lows),
  runs = lapply(c$runs, function(run) lapply(run, function(p)
    list(x = num(p[[1]]), y = num(p[[2]]), kind = as.character(p[[3]])))),
  icons = c$icons, grades = c$grades))
dates <- as.character(unlist(d$dates))
y_min <- num(d$y_min); y_max <- num(d$y_max)
cascade_note <- if (is.null(d$cascade_note)) '' else as.character(d$cascade_note)
title_suffix <- if (is.null(d$title_suffix)) '' else as.character(d$title_suffix)
confidence_source <- if (is.null(d$confidence_source)) 'IFSアンサンブル' else as.character(d$confidence_source)

CITY_COLOURS <- c('#1F77B4', '#FF7F0E')  # matplotlib C0/C1: Tokyo, Hong Kong
FONT <- 'Noto Sans CJK JP'

sample_curve <- function(px, py, n_min = 60) {
  if (length(px) < 2) return(NULL)
  n <- max(n_min, as.integer((px[length(px)] - px[1]) * 60))
  xs <- seq(px[1], px[length(px)], length.out = n)
  list(x = xs, y = splinefun(px, py, method = 'monoH.FC')(xs))
}
eval_held <- function(px, py, grid) {
  if (length(px) == 1) return(rep(py, length(grid)))
  splinefun(px, py, method = 'monoH.FC')(pmin(pmax(grid, px[1]), px[length(px)]))
}

ribbon_rows <- list(); line_rows <- list(); point_rows <- list()
for (ci in seq_along(cities)) {
  city <- cities[[ci]]
  highs_label <- paste0(city$label, ' 最高'); lows_label <- paste0(city$label, ' 最低')
  for (run in city$runs) {
    hp <- Filter(function(p) p$kind == 'high', run)
    lp <- Filter(function(p) p$kind == 'low', run)
    hx <- sapply(hp, `[[`, 'x'); hy <- sapply(hp, `[[`, 'y')
    lx <- sapply(lp, `[[`, 'x'); ly <- sapply(lp, `[[`, 'y')
    if (length(hp) && length(lp) && run[[length(run)]]$x > run[[1]]$x) {
      x0 <- run[[1]]$x; x1 <- run[[length(run)]]$x
      grid <- seq(x0, x1, length.out = max(60, as.integer((x1 - x0) * 60)))
      upper <- eval_held(hx, hy, grid); lower <- eval_held(lx, ly, grid)
      keep <- upper >= lower
      ribbon_rows[[length(ribbon_rows) + 1]] <- data.frame(
        x = grid[keep], ymin = lower[keep], ymax = upper[keep],
        city = city$label, stringsAsFactors = FALSE)
    }
    sc <- sample_curve(hx, hy)
    if (!is.null(sc)) line_rows[[length(line_rows) + 1]] <- data.frame(
      x = sc$x, y = sc$y, series = highs_label, stringsAsFactors = FALSE)
    sc <- sample_curve(lx, ly)
    if (!is.null(sc)) line_rows[[length(line_rows) + 1]] <- data.frame(
      x = sc$x, y = sc$y, series = lows_label, stringsAsFactors = FALSE)
    if (length(hp)) point_rows[[length(point_rows) + 1]] <- data.frame(
      x = hx, y = hy, city = city$label, kind = 'high', stringsAsFactors = FALSE)
    if (length(lp)) point_rows[[length(point_rows) + 1]] <- data.frame(
      x = lx, y = ly, city = city$label, kind = 'low', stringsAsFactors = FALSE)
  }
}
ribbons <- do.call(rbind, ribbon_rows)
lines <- do.call(rbind, line_rows)
points <- do.call(rbind, point_rows)
city_labels <- sapply(cities, `[[`, 'label')
series_levels <- c(paste0(city_labels[1], ' 最高'), paste0(city_labels[2], ' 最高'),
                   paste0(city_labels[1], ' 最低'), paste0(city_labels[2], ' 最低'))
lines$series <- factor(lines$series, levels = series_levels)
points$city <- factor(points$city, levels = city_labels)
ribbons$city <- factor(ribbons$city, levels = city_labels)
# One named colour map covering BOTH mapped variables (the line series and
# the point/ribbon city) so the manual scales cannot drop a level to NA.
COLOUR_MAP <- c(CITY_COLOURS[1], CITY_COLOURS[2], CITY_COLOURS[1], CITY_COLOURS[2],
                CITY_COLOURS[1], CITY_COLOURS[2])
names(COLOUR_MAP) <- c(series_levels, city_labels)
major_y <- seq(ceiling(y_min / 5) * 5, y_max, by = 5)

p <- ggplot() +
  geom_ribbon(data = ribbons, aes(x = x, ymin = ymin, ymax = ymax, fill = city),
              alpha = 0.13, linewidth = 0) +
  geom_line(data = lines[lines$series %in% series_levels[c(1, 2)], ],
            aes(x = x, y = y, colour = series, linetype = series), linewidth = 1.05) +
  geom_line(data = lines[lines$series %in% series_levels[c(3, 4)], ],
            aes(x = x, y = y, colour = series, linetype = series), linewidth = 0.85) +
  geom_point(data = points[points$kind == 'high', ], aes(x = x, y = y, colour = city),
             size = 1.35, alpha = 0.95, show.legend = FALSE) +
  geom_point(data = points[points$kind == 'low', ], aes(x = x, y = y, colour = city),
             size = 1.15, alpha = 0.95, show.legend = FALSE) +
  scale_fill_manual(values = COLOUR_MAP, guide = 'none') +
  scale_colour_manual(values = COLOUR_MAP, breaks = series_levels) +
  scale_linetype_manual(values = c('solid', 'solid', 'dashed', 'dashed'),
                        breaks = series_levels) +
  scale_x_continuous(breaks = 0:27, labels = dates, limits = c(-0.08, 28.0),
                     expand = c(0, 0)) +
  scale_y_continuous(breaks = major_y, minor_breaks = seq(y_min, y_max, by = 1),
                     limits = c(y_min, y_max), expand = c(0, 0)) +
  labs(x = NULL, y = '気温（℃）', colour = NULL, linetype = NULL) +
  guides(colour = guide_legend(ncol = 2, byrow = TRUE,
                               override.aes = list(linewidth = c(1.05, 1.05, 0.85, 0.85))),
         linetype = guide_legend(ncol = 2, byrow = TRUE,
                                 override.aes = list(linewidth = c(1.05, 1.05, 0.85, 0.85)))) +
  theme_bw(base_family = FONT, base_size = 11) +
  theme(panel.grid.major.x = element_blank(),
        panel.grid.minor.x = element_blank(),
        panel.grid.major.y = element_line(colour = '#00000033', linewidth = 0.25),
        panel.grid.minor.y = element_line(colour = '#0000001C', linewidth = 0.18),
        panel.border = element_blank(),
        axis.line = element_line(colour = 'black', linewidth = 0.3),
        axis.ticks.x = element_blank(),
        axis.ticks.y = element_line(colour = 'black', linewidth = 0.3),
        axis.ticks.length = unit(0.12, 'cm'),
        axis.text.x = element_text(size = 11, margin = margin(t = 6)),
        axis.text.y = element_text(size = 10.5),
        axis.title.y = element_text(size = 11.5),
        legend.position = c(0.995, 0.995), legend.justification = c(1, 1),
        legend.background = element_blank(), legend.key = element_blank(),
        legend.text = element_text(size = 11), legend.spacing.x = unit(0.35, 'cm'),
        plot.margin = margin(2, 2, 0, 0))

# Figure geometry, in inches on the 20 x 7.2 canvas (mirrors the retired
# renderer's figure fractions: panel left .125, bottom .27, width .845,
# height .60; the ggplot viewport is grown by the label allowances so its
# panel lands on that rect).
W <- 20; H <- 7.2
PANEL <- list(x = 0.125 * W, y = 0.27 * H, w = 0.845 * W, h = 0.60 * H)
ROW_Y <- c(0.16 * H, 0.105 * H)

emoji_drawn <- 0
draw_figure <- function() {
  grid.newpage()
  pushViewport(viewport(x = unit(PANEL$x - 0.55, 'in'), y = unit(PANEL$y - 0.30, 'in'),
                        width = unit(PANEL$w + 0.60, 'in'),
                        height = unit(PANEL$h + 0.30, 'in'),
                        just = c('left', 'bottom')))
  grid.draw(ggplotGrob(p))
  popViewport()
  txt <- function(label, x, y, size, ...) grid.text(
    label, x = unit(x, 'in'), y = unit(y, 'in'), just = 'left',
    gp = gpar(fontfamily = FONT, fontsize = size, ...))
  txt(paste0('香港・東京都港区｜28日間気温予報', title_suffix), PANEL$x, 0.947 * H, 20, fontface = 'bold')
  txt('実線：日中最高（当日）／破線：夜間最低（日付間・当日正午～翌日正午の最低）｜淡色＝高低の連続帯',
      PANEL$x, 0.908 * H, 10.8)
  txt(paste0('日付位置は時刻を示さない。下段の自信度は', confidence_source, 'の日最高気温の拡散より。気温の接続順は下部のデータ源注記参照。'),
      PANEL$x, 0.882 * H, 9.6, col = '#595959')
  for (ri in seq_along(cities)) {
    city <- cities[[ri]]
    grid.text(city$label, x = unit(0.024 * W, 'in'), y = unit(ROW_Y[ri], 'in'),
              just = 'left', gp = gpar(fontfamily = FONT, fontsize = 13,
                                       fontface = 'bold', col = CITY_COLOURS[ri]))
    for (i in 0:27) {
      cx <- PANEL$x + PANEL$w * (i / 28)
      icon <- city$icons[[i + 1]]
      grade <- city$grades[[i + 1]]
      grade_txt <- if (is.null(grade)) '未評価' else as.character(grade)
      asset <- icon$asset
      if (!is.null(asset) && !is.null(icon$emoji) && icon$emoji != '―') {
        img <- readPNG(file.path(assets_dir, paste0(asset, '.png')))
        h_in <- 46 / 170
        w_in <- h_in * dim(img)[2] / dim(img)[1]
        grid.raster(img, x = unit(cx - 22 / 170, 'in'), y = unit(ROW_Y[ri], 'in'),
                    width = unit(w_in, 'in'), height = unit(h_in, 'in'))
        emoji_drawn <<- emoji_drawn + 1
        txt(paste0('/', grade_txt), cx + 0.005 * W, ROW_Y[ri], 10.5)
      } else {
        grid.text(paste0('―/', grade_txt), x = unit(cx, 'in'),
                  y = unit(ROW_Y[ri], 'in'), just = 'center',
                  gp = gpar(fontfamily = FONT, fontsize = 10.5))
      }
    }
  }
  if (nzchar(cascade_note)) txt(cascade_note, PANEL$x, 0.068 * H, 9.8, col = '#444444')
  txt('各日の最高：その日の予報最大値　／　夜間の最低：両日正午に挟まれた24時間の予報最小値',
      PANEL$x, 0.04 * H, 10, col = '#555555')
}

agg_png(output_path, width = W, height = H, units = 'in', res = 170,
        background = 'white')
draw_figure()
dev.off()
emoji_png <- emoji_drawn
emoji_drawn <- 0
svglite(sub('\\.png$', '.svg', output_path), width = W, height = H, bg = 'white')
draw_figure()
dev.off()
cat(toJSON(list(emoji_composited = emoji_png), auto_unbox = TRUE), '\n')
