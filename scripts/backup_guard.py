#!/usr/bin/env python3
"""Bind the scheduled backup to one report date and check publication."""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import subprocess

from collect import CITIES, JST, UTC, parse_time
from publish_data import data_manifest, fetch_data_head, git
from read_bundle import validate_calculated, validate_manifest_identity, verify_bytes

BACKUP_CRON = '5 20 * * *'
MAX_SCHEDULE_DELAY = dt.timedelta(hours=12)


def scheduled_report_date(schedule, created_at):
    if schedule != BACKUP_CRON:
        raise ValueError(f'Unknown backup schedule {schedule!r} for run created at {created_at}.')
    created = dt.datetime.fromisoformat(created_at)
    if created.tzinfo is None:
        raise ValueError(f'Backup run timestamp has no timezone: {created_at!r}.')
    created = created.astimezone(UTC)
    minute, hour, *_ = schedule.split()
    nominal = created.replace(hour=int(hour), minute=int(minute), second=0, microsecond=0)
    if created < nominal:
        nominal -= dt.timedelta(days=1)
    day = nominal.astimezone(JST).date()
    if created - nominal > MAX_SCHEDULE_DELAY:
        raise ValueError(f'Cannot bind backup created at {created.astimezone(JST).isoformat()} '
                         f'to the {day} 05:05 JST slot within its 12-hour delay window.')
    return day


def verify_report_json(commit, manifest, day):
    validate_manifest_identity(manifest, day.isoformat())
    start = dt.datetime.combine(day, dt.time(6), JST)
    if (parse_time(manifest['window_start_utc']) != start
            or parse_time(manifest['window_end_utc']) != start + dt.timedelta(days=1)):
        raise ValueError('WRONG_REPORT_WINDOW')
    parse_time(manifest['collection_utc'])
    if not isinstance(manifest['quality']['ready'], bool):
        raise ValueError('INVALID_REPORT_QUALITY')

    for key in (*CITIES, 'official', 'regional', 'calculated'):
        entry = manifest['files'][key]
        path = f'latest/{key}.json'
        if entry['path'] != path:
            raise ValueError(f'UNEXPECTED_DATASET_PATH: {key}')
        blob = git('cat-file', 'blob', f'{commit}:{path}', text=False).stdout
        verify_bytes(blob, entry)
        record = json.loads(blob)
        if not isinstance(record, dict):
            raise ValueError(f'INVALID_DATASET: {key}')
        if key in CITIES and record.get('key') != key:
            raise ValueError(f'CITY_IDENTITY_MISMATCH: {key}')
        if key == 'calculated':
            validate_calculated(record, manifest)


def should_collect(day):
    commit = fetch_data_head()
    if commit is None:
        print(f'No data branch. Collecting {day}.')
        return True
    manifest, published_date = data_manifest(commit)
    if published_date > day:
        print(f'Skipping {day}: latest is already dated {published_date} at {commit}.')
        return False
    if published_date < day:
        print(f'Latest is dated {published_date} at {commit}. Collecting {day}.')
        return True
    try:
        verify_report_json(commit, manifest, day)
    except (AttributeError, KeyError, TypeError, ValueError, subprocess.CalledProcessError) as exc:
        print(f'Published JSON for {day} at {commit} is missing or invalid ({exc}). Collecting {day}.')
        return True
    print(f'Skipping {day}: the report JSON is published at {commit}.')
    return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--schedule', required=True)
    parser.add_argument('--created-at', required=True)
    args = parser.parse_args()
    try:
        day = scheduled_report_date(args.schedule, args.created_at)
        proceed = should_collect(day)
    except (ValueError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f'Backup guard failed: {exc}\n')
    with Path(os.environ['GITHUB_OUTPUT']).open('a', encoding='utf-8') as out:
        out.write(f'target_date_jst={day.isoformat()}\nproceed={str(proceed).lower()}\n')


if __name__ == '__main__':
    main()
