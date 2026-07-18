import logging

audit_logger = logging.getLogger('audit')


def log_admin_action(
    admin_id: int,
    action: str,
    target: str = '',
    details: str = '',
) -> None:
    """Logs an admin action for audit trail."""
    audit_logger.info(
        'ADMIN_ACTION admin=%s action=%s target=%s details=%s',
        admin_id, action, target, details,
    )
