"""Disk checks for collection and backup; byte counts include user-available space."""
import logging
import shutil

GIB = 1024**3
MIB = 1024**2
log = logging.getLogger('muse.storage')


def disk_status(path):
    usage = shutil.disk_usage(path)
    warn = max(4 * GIB, usage.total * 15 // 100)
    critical = max(GIB, usage.total * 5 // 100)
    state = 'critical' if usage.free <= critical else 'warning' if usage.free <= warn else 'ok'
    return {'level': state, 'free_bytes': usage.free, 'total_bytes': usage.total,
            'warn_below_bytes': warn, 'critical_below_bytes': critical}


def report_collection(path):
    report = disk_status(path)
    level = report['level']
    if level != 'ok':
        log.log(logging.CRITICAL if level == 'critical' else logging.WARNING,
                'Muse disk %s: free=%s bytes, warning threshold=%s bytes',
                level, report['free_bytes'], report['warn_below_bytes'])
    return report


def require_backup_space(path, copy_bytes):
    report = disk_status(path)
    reserve = max(2 * GIB, report['critical_below_bytes'])
    # Allow for growth during online copy and staging overhead.
    required = reserve + copy_bytes + max(64 * MIB, copy_bytes // 4)
    if report['free_bytes'] < required:
        raise RuntimeError(f'Backup skipped: free={report["free_bytes"]} bytes, required={required} bytes '
                           f'(copy={copy_bytes}, reserve={reserve})')
    return report['free_bytes'], required
