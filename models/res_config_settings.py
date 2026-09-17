import logging
from odoo import api, fields, models
from .nextcloud_client import NextcloudWebDAVClient

_logger = logging.getLogger(__name__)


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    # Nextcloud Configuration
    data_sentinel_nextcloud_url = fields.Char(
        string="Nextcloud WebDAV URL",
        config_parameter="data_sentinel.nextcloud_url",
        default="https://vmi3020185.contaboserver.net/remote.php/dav/files/admin",
        help="Full WebDAV URL endpoint for Nextcloud (e.g. https://domain.com/remote.php/dav/files/admin)",
    )
    data_sentinel_nextcloud_user = fields.Char(
        string="Nextcloud Username",
        config_parameter="data_sentinel.nextcloud_user",
        default="admin",
    )
    data_sentinel_nextcloud_password = fields.Char(
        string="Nextcloud Password / App Password",
        config_parameter="data_sentinel.nextcloud_password",
        default="Farai@#$1234",
    )
    data_sentinel_nextcloud_root_dir = fields.Char(
        string="Nextcloud Root Folder",
        config_parameter="data_sentinel.nextcloud_root_dir",
        default="DataSentinel",
        help="Directory name created inside Nextcloud to store all backups.",
    )
    data_sentinel_verify_ssl = fields.Boolean(
        string="Verify SSL Certificates",
        config_parameter="data_sentinel.verify_ssl",
        default=False,
    )

    # Full Instance Backup Scheduler (Midnight)
    data_sentinel_full_active = fields.Boolean(
        string="Enable Daily Full Backup",
        config_parameter="data_sentinel.full_active",
        default=True,
    )
    data_sentinel_full_time = fields.Float(
        string="Full Backup Run Time (Hour)",
        config_parameter="data_sentinel.full_time",
        default=0.0,
        help="Hour in 24h format (e.g., 0.0 for 12:00 AM Midnight, 2.5 for 2:30 AM).",
    )
    data_sentinel_full_include_db = fields.Boolean(
        string="Include Database in Full Backup",
        config_parameter="data_sentinel.full_include_db",
        default=True,
    )
    data_sentinel_full_include_filestore = fields.Boolean(
        string="Include Filestore in Full Backup",
        config_parameter="data_sentinel.full_include_filestore",
        default=True,
    )
    data_sentinel_full_include_addons = fields.Boolean(
        string="Include Custom Addons in Full Backup",
        config_parameter="data_sentinel.full_include_addons",
        default=True,
    )
    data_sentinel_full_retention_days = fields.Integer(
        string="Full Backup Cloud Retention (Days)",
        config_parameter="data_sentinel.full_retention_days",
        default=30,
        help="Delete full backups on Nextcloud older than this number of days (0 = Keep forever).",
    )

    # Timely Snapshot Backup Scheduler (Working Hours)
    data_sentinel_timely_active = fields.Boolean(
        string="Enable Timely Snapshot Backups",
        config_parameter="data_sentinel.timely_active",
        default=True,
    )
    data_sentinel_timely_interval_hours = fields.Integer(
        string="Snapshot Interval (Hours)",
        config_parameter="data_sentinel.timely_interval_hours",
        default=3,
        help="Interval in hours between snapshots (e.g., 3 for every 3 hours).",
    )
    data_sentinel_timely_start_hour = fields.Float(
        string="Snapshot Window Start Hour",
        config_parameter="data_sentinel.timely_start_hour",
        default=5.0,
        help="Earliest hour to run timely backups (e.g. 5.0 for 5:00 AM).",
    )
    data_sentinel_timely_end_hour = fields.Float(
        string="Snapshot Window End Hour",
        config_parameter="data_sentinel.timely_end_hour",
        default=20.0,
        help="Latest hour to run timely backups (e.g. 20.0 for 8:00 PM).",
    )
    data_sentinel_timely_include_db = fields.Boolean(
        string="Include Database in Snapshot",
        config_parameter="data_sentinel.timely_include_db",
        default=True,
    )
    data_sentinel_timely_include_filestore = fields.Boolean(
        string="Include Filestore in Snapshot",
        config_parameter="data_sentinel.timely_include_filestore",
        default=True,
    )
    data_sentinel_timely_retention_days = fields.Integer(
        string="Snapshot Cloud Retention (Days)",
        config_parameter="data_sentinel.timely_retention_days",
        default=14,
        help="Delete snapshots on Nextcloud older than this number of days (0 = Keep forever).",
    )

    # Local Retention
    data_sentinel_keep_local_copy = fields.Boolean(
        string="Keep Local Copy on Server",
        config_parameter="data_sentinel.keep_local_copy",
        default=False,
        help="If disabled, backup files are deleted locally immediately after successful Nextcloud upload.",
    )
    data_sentinel_local_retention_count = fields.Integer(
        string="Local Backups to Keep",
        config_parameter="data_sentinel.local_retention_count",
        default=5,
    )

    def action_test_nextcloud_connection(self):
        """Test Nextcloud WebDAV credentials and connection."""
        self.ensure_one()
        url = self.data_sentinel_nextcloud_url or self.env['ir.config_parameter'].sudo().get_param('data_sentinel.nextcloud_url')
        user = self.data_sentinel_nextcloud_user or self.env['ir.config_parameter'].sudo().get_param('data_sentinel.nextcloud_user')
        password = self.data_sentinel_nextcloud_password or self.env['ir.config_parameter'].sudo().get_param('data_sentinel.nextcloud_password')
        verify_ssl = self.data_sentinel_verify_ssl

        client = NextcloudWebDAVClient(url, user, password, verify_ssl=verify_ssl)
        success, message = client.test_connection()

        if success:
            # Also ensure the base root directory exists
            root_dir = self.data_sentinel_nextcloud_root_dir or "DataSentinel"
            client.ensure_directory(root_dir)
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": "Nextcloud Connection Success",
                    "message": f"{message} Root directory '{root_dir}' verified.",
                    "type": "success",
                    "sticky": False,
                },
            }
        else:
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": "Nextcloud Connection Failed",
                    "message": message,
                    "type": "danger",
                    "sticky": True,
                },
            }
