{
    "name": "Data Sentinel - Automated Nextcloud Backup",
    "version": "19.0.1.0.0",
    "category": "Administration/Backup",
    "summary": "Automated Full Instance & Timely Backups to Nextcloud with configurable intervals, filestore, custom addons, and audit logs.",
    "description": """
Data Sentinel - Enterprise Nextcloud Backup & Disaster Recovery
==============================================================

Automate Odoo database, filestore, and custom addons backups directly to Nextcloud WebDAV.

Key Features:
-------------
* **Full Instance Backups**: Scheduled daily (e.g. 12:00 AM Midnight) including Postgres DB, Odoo Filestore, Custom Addons, and Manifest metadata.
* **Timely Backups**: High-frequency snapshot backups (DB + Filestore) at configurable intervals (e.g. every 3 hours between 5:00 AM and 8:00 PM).
* **Nextcloud WebDAV Integration**: Direct streaming upload to Nextcloud with automatic folder structure creation (`DataSentinel/<dbname>/Full/` and `DataSentinel/<dbname>/Timely/`).
* **Dynamic Settings**: Configurable WebDAV credentials, custom schedules, and retention policies with zero hardcoding.
* **Audit Logs & History**: Full audit trail of all backups with file sizes, execution durations, remote URLs, and error logs.
* **Manual Controls**: On-demand backup triggers, one-click re-upload, and direct archive download.
    """,
    "author": "DraftPOS / Havano",
    "website": "https://havano.pro",
    "license": "LGPL-3",
    "depends": ["base", "web", "base_setup"],
    "data": [
        "security/data_sentinel_security.xml",
        "security/ir.model.access.csv",
        "data/ir_cron_data.xml",
        "views/data_sentinel_backup_views.xml",
        "views/res_config_settings_views.xml",
        "views/data_sentinel_menus.xml",
    ],
    "images": ["static/description/icon.png"],
    "installable": True,
    "application": True,
    "auto_install": False,
}
