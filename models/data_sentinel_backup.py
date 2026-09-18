import io
import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
import zipfile
from datetime import datetime, timedelta

import odoo
from odoo import api, fields, models, _
from odoo.exceptions import UserError
from .nextcloud_client import NextcloudWebDAVClient

_logger = logging.getLogger(__name__)


class DataSentinelBackup(models.Model):
    _name = "data.sentinel.backup"
    _description = "Data Sentinel Backup Record"
    _order = "create_date desc, id desc"

    name = fields.Char(string="Backup Reference", required=True, copy=False, default="New")
    backup_type = fields.Selection(
        [
            ("full", "Full Instance Backup"),
            ("timely", "Timely Snapshot (DB + Filestore)"),
            ("manual", "Manual On-Demand"),
        ],
        string="Backup Type",
        required=True,
        default="full",
    )
    trigger_source = fields.Selection(
        [
            ("cron", "Automated Scheduler"),
            ("manual", "Manual Trigger"),
        ],
        string="Trigger Source",
        required=True,
        default="manual",
    )
    state = fields.Selection(
        [
            ("draft", "Pending"),
            ("running", "In Progress"),
            ("success", "Completed & Uploaded"),
            ("local_only", "Saved Locally (Upload Pending)"),
            ("failed", "Failed"),
        ],
        string="Status",
        default="draft",
        required=True,
        index=True,
    )
    db_name = fields.Char(string="Database Name", required=True, default=lambda self: self.env.cr.dbname)
    include_db = fields.Boolean(string="Include Database", default=True)
    include_filestore = fields.Boolean(string="Include Filestore", default=True)
    include_addons = fields.Boolean(string="Include Custom Addons", default=True)

    file_name = fields.Char(string="Archive Filename")
    file_size = fields.Float(string="File Size (Bytes)", default=0.0)
    file_size_display = fields.Char(string="File Size", compute="_compute_file_size_display")

    start_time = fields.Datetime(string="Start Time")
    end_time = fields.Datetime(string="End Time")
    duration_seconds = fields.Float(string="Duration (Seconds)", default=0.0)
    duration_display = fields.Char(string="Duration", compute="_compute_duration_display")

    local_path = fields.Char(string="Local Storage Path")
    nextcloud_path = fields.Char(string="Nextcloud Remote Path")
    nextcloud_uploaded = fields.Boolean(string="Uploaded to Nextcloud", default=False)
    nextcloud_url = fields.Char(string="Nextcloud WebDAV URL")

    log_details = fields.Text(string="Execution Log")
    error_details = fields.Text(string="Error Details")

    @api.depends("file_size")
    def _compute_file_size_display(self):
        for rec in self:
            size = rec.file_size or 0.0
            if size <= 0:
                rec.file_size_display = "0 B"
            elif size < 1024:
                rec.file_size_display = f"{size:.0f} B"
            elif size < 1024 * 1024:
                rec.file_size_display = f"{size / 1024:.2f} KB"
            elif size < 1024 * 1024 * 1024:
                rec.file_size_display = f"{size / (1024 * 1024):.2f} MB"
            else:
                rec.file_size_display = f"{size / (1024 * 1024 * 1024):.2f} GB"

    @api.depends("duration_seconds")
    def _compute_duration_display(self):
        for rec in self:
            d = rec.duration_seconds or 0.0
            if d < 60:
                rec.duration_display = f"{d:.1f}s"
            elif d < 3600:
                rec.duration_display = f"{int(d // 60)}m {int(d % 60)}s"
            else:
                rec.duration_display = f"{int(d // 3600)}h {int((d % 3600) // 60)}m"

    def _append_log(self, message):
        """Append timestamped log line to execution log and commit to DB."""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_line = f"[{timestamp}] {message}\n"
        self.log_details = (self.log_details or "") + log_line
        _logger.info("[DataSentinel %s] %s", self.name or "Backup", message)
        try:
            self.env.cr.commit()
        except Exception:
            pass

    def action_run_backup(self):
        """Execute backup process for this record."""
        self.ensure_one()
        return self._execute_backup()

    def action_retry_upload(self):
        """Retry uploading an existing local backup archive to Nextcloud."""
        self.ensure_one()
        if not self.local_path or not os.path.exists(self.local_path):
            raise UserError(_("Local backup file not found on server at %s") % (self.local_path or "N/A"))

        config = self.env["ir.config_parameter"].sudo()
        url = config.get_param("data_sentinel.nextcloud_url")
        user = config.get_param("data_sentinel.nextcloud_user")
        password = config.get_param("data_sentinel.nextcloud_password")
        verify_ssl = config.get_param("data_sentinel.verify_ssl") == "True"

        client = NextcloudWebDAVClient(url, user, password, verify_ssl=verify_ssl)
        self._append_log(f"Retrying upload to Nextcloud: {self.nextcloud_path}...")

        success, msg = client.upload_file(self.local_path, self.nextcloud_path)
        if success:
            self.write({
                "state": "success",
                "nextcloud_uploaded": True,
            })
            self._append_log("Upload succeeded on retry.")
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": "Upload Succeeded",
                    "message": "Backup archive successfully uploaded to Nextcloud.",
                    "type": "success",
                    "sticky": False,
                },
            }
        else:
            self._append_log(f"Upload failed: {msg}")
            raise UserError(_("Nextcloud upload failed: %s") % msg)

    def action_purge_backup(self):
        """Permanently delete backup archive from Nextcloud and local server disk."""
        nc_url = self.env["ir.config_parameter"].sudo().get_param("data_sentinel.nextcloud_url")
        nc_user = self.env["ir.config_parameter"].sudo().get_param("data_sentinel.nextcloud_user")
        nc_pass = self.env["ir.config_parameter"].sudo().get_param("data_sentinel.nextcloud_password")
        verify_ssl = self.env["ir.config_parameter"].sudo().get_param("data_sentinel.verify_ssl") == "True"
        client = NextcloudWebDAVClient(nc_url, nc_user, nc_pass, verify_ssl=verify_ssl) if nc_url and nc_user else None

        for rec in self:
            # Delete local file if present
            if rec.local_path and os.path.exists(rec.local_path):
                try:
                    os.remove(rec.local_path)
                except Exception as ex:
                    _logger.warning("Failed deleting local backup file %s: %s", rec.local_path, str(ex))

            # Delete Nextcloud remote file
            if rec.nextcloud_path and client:
                try:
                    client.delete_file(rec.nextcloud_path)
                except Exception as ex:
                    _logger.warning("Failed deleting remote Nextcloud file %s: %s", rec.nextcloud_path, str(ex))

            rec.unlink()

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Backup Purged",
                "message": "Backup record and all associated files have been permanently deleted from storage.",
                "type": "info",
                "sticky": False,
            },
        }

    def _execute_backup(self):
        """Main backup routine executing DB dump, filestore, custom addons, and Nextcloud WebDAV upload."""
        self.ensure_one()
        db_name = self.db_name or self.env.cr.dbname
        now = datetime.now()
        timestamp_str = now.strftime("%Y%m%d_%H%M%S")

        # Generate unique file name
        type_tag = "full" if self.backup_type == "full" else "timely"
        archive_name = f"{db_name}_{type_tag}_{timestamp_str}.zip"
        self.file_name = archive_name
        self.name = f"{db_name.upper()}-{type_tag.upper()}-{timestamp_str}"
        self.start_time = fields.Datetime.now()
        self.state = "running"
        self.log_details = ""
        self._append_log(f"Starting {self.backup_type.upper()} backup for database: {db_name}")

        temp_dir = tempfile.mkdtemp(prefix=f"data_sentinel_{db_name}_")
        local_archive_path = os.path.join(temp_dir, archive_name)
        start_clock = time.time()

        try:
            with zipfile.ZipFile(local_archive_path, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zip_out:

                # -------------------------------------------------------------
                # 1. DATABASE DUMP (Direct PostgreSQL pg_dump)
                # -------------------------------------------------------------
                if self.include_db:
                    self._append_log("Dumping PostgreSQL database using pg_dump...")
                    db_dump_file = os.path.join(temp_dir, "dump.sql")
                    cmd = ["pg_dump", "--no-owner", db_name]
                    env_vars = os.environ.copy()
                    
                    db_user = odoo.tools.config.get("db_user")
                    if db_user:
                        cmd.extend(["-U", str(db_user)])
                        env_vars["PGUSER"] = str(db_user)

                    db_host = odoo.tools.config.get("db_host")
                    if db_host:
                        cmd.extend(["-h", str(db_host)])
                        env_vars["PGHOST"] = str(db_host)

                    db_port = odoo.tools.config.get("db_port")
                    if db_port:
                        cmd.extend(["-p", str(db_port)])
                        env_vars["PGPORT"] = str(db_port)

                    db_password = odoo.tools.config.get("db_password")
                    if db_password:
                        env_vars["PGPASSWORD"] = str(db_password)

                    with open(db_dump_file, "wb") as f_out:
                        p = subprocess.Popen(cmd, stdout=f_out, stderr=subprocess.PIPE, env=env_vars)
                        _, err = p.communicate()
                        if p.returncode != 0:
                            raise Exception(f"pg_dump error: {err.decode('utf-8', errors='ignore')}")

                    zip_out.write(db_dump_file, "dump.sql")
                    db_size_mb = os.path.getsize(db_dump_file) / (1024 * 1024)
                    self._append_log(f"Database dump complete ({db_size_mb:.2f} MB).")
                    if os.path.exists(db_dump_file):
                        os.remove(db_dump_file)

                # -------------------------------------------------------------
                # 2. FILESTORE ARCHIVING
                # -------------------------------------------------------------
                if self.include_filestore:
                    self._append_log("Archiving Odoo filestore...")
                    filestore_path = odoo.tools.config.filestore(db_name)
                    if not os.path.exists(filestore_path):
                        filestore_path = os.path.join("/var/lib/odoo/filestore", db_name)

                    if os.path.exists(filestore_path):
                        file_count = 0
                        for root, _, files in os.walk(filestore_path):
                            for file in files:
                                file_full_path = os.path.join(root, file)
                                rel_path = os.path.relpath(file_full_path, filestore_path)
                                arcname = os.path.join("filestore", rel_path)
                                zip_out.write(file_full_path, arcname)
                                file_count += 1
                        self._append_log(f"Filestore archived successfully ({file_count} files).")
                    else:
                        self._append_log(f"Filestore path not found: {filestore_path} (Skipping).")

                # -------------------------------------------------------------
                # 2. CUSTOM ADDONS ARCHIVING (Full Backup)
                # -------------------------------------------------------------
                if self.include_addons and self.backup_type == "full":
                    self._append_log("Archiving custom addons from /mnt/extra-addons...")
                    custom_dir = "/mnt/extra-addons"
                    if os.path.exists(custom_dir):
                        addon_count = 0
                        for root, dirs, files in os.walk(custom_dir):
                            # Skip heavy/unnecessary folders
                            dirs[:] = [d for d in dirs if d not in [".git", "node_modules", "__pycache__", ".vscode", ".idea", ".tempmediaStorage"]]
                            for file in files:
                                if file.endswith(".pyc") or file.endswith(".pyo") or file.endswith(".swp"):
                                    continue
                                file_full_path = os.path.join(root, file)
                                rel_path = os.path.relpath(file_full_path, custom_dir)
                                arcname = os.path.join("custom_addons", rel_path)
                                zip_out.write(file_full_path, arcname)
                                addon_count += 1
                        self._append_log(f"Custom addons archived successfully ({addon_count} files).")
                    else:
                        self._append_log(f"Custom addons directory {custom_dir} not found (Skipping).")

                # -------------------------------------------------------------
                # 4. MANIFEST METADATA
                # -------------------------------------------------------------
                manifest_data = {
                    "backup_reference": self.name,
                    "database": db_name,
                    "backup_type": self.backup_type,
                    "created_at": datetime.now().isoformat(),
                    "server_version": odoo.release.version,
                    "include_db": self.include_db,
                    "include_filestore": self.include_filestore,
                    "include_addons": self.include_addons,
                }
                zip_out.writestr("data_sentinel_manifest.json", json.dumps(manifest_data, indent=2))

            # File stats
            archive_size = os.path.getsize(local_archive_path)
            self.file_size = archive_size
            self.local_path = local_archive_path
            self._append_log(f"ZIP Archive created successfully. Total size: {self.file_size_display}")

            # -------------------------------------------------------------
            # 5. NEXTCLOUD WEBDAV UPLOAD
            # -------------------------------------------------------------
            config = self.env["ir.config_parameter"].sudo()
            nc_url = config.get_param("data_sentinel.nextcloud_url", "https://vmi3020185.contaboserver.net/remote.php/dav/files/admin")
            nc_user = config.get_param("data_sentinel.nextcloud_user", "admin")
            nc_pass = config.get_param("data_sentinel.nextcloud_password", "Farai@#$1234")
            nc_root = config.get_param("data_sentinel.nextcloud_root_dir", "DataSentinel")
            verify_ssl = config.get_param("data_sentinel.verify_ssl") == "True"
            keep_local = config.get_param("data_sentinel.keep_local_copy") == "True"

            subfolder = "Full" if self.backup_type == "full" else "Timely"
            remote_relative_path = f"{nc_root}/{db_name}/{subfolder}/{archive_name}"
            self.nextcloud_path = remote_relative_path
            self.nextcloud_url = f"{nc_url.rstrip('/')}/{remote_relative_path.strip('/')}"

            self._append_log(f"Initiating WebDAV upload to Nextcloud: {remote_relative_path}...")
            client = NextcloudWebDAVClient(nc_url, nc_user, nc_pass, verify_ssl=verify_ssl)

            upload_success, upload_msg = client.upload_file(local_archive_path, remote_relative_path)

            end_clock = time.time()
            duration = end_clock - start_clock
            self.end_time = fields.Datetime.now()
            self.duration_seconds = duration

            if upload_success:
                self.state = "success"
                self.nextcloud_uploaded = True
                self._append_log(f"Nextcloud upload completed successfully in {duration:.1f}s. {upload_msg}")

                # Clean local file if keep_local_copy is False
                if not keep_local:
                    try:
                        if os.path.exists(local_archive_path):
                            os.remove(local_archive_path)
                        shutil.rmtree(temp_dir, ignore_errors=True)
                        self.local_path = False
                        self._append_log("Temporary local archive cleaned up after successful cloud upload.")
                    except Exception as clean_err:
                        self._append_log(f"Warning during temp cleanup: {clean_err}")
            else:
                self.state = "local_only"
                self.nextcloud_uploaded = False
                self.error_details = upload_msg
                self._append_log(f"Upload to Nextcloud failed ({upload_msg}). Archive retained locally at: {local_archive_path}")

        except Exception as ex:
            _logger.exception("Data Sentinel Backup Error: %s", str(ex))
            self.state = "failed"
            self.end_time = fields.Datetime.now()
            self.duration_seconds = time.time() - start_clock
            self.error_details = str(ex)
            self._append_log(f"FATAL ERROR during backup execution: {str(ex)}")

        return True

    # -------------------------------------------------------------------------
    # CRON SCHEDULER METHODS
    # -------------------------------------------------------------------------
    @api.model
    def _cron_full_backup(self):
        """Automated daily full backup executed at midnight (or configured time)."""
        config = self.env["ir.config_parameter"].sudo()
        if config.get_param("data_sentinel.full_active", "True") != "True":
            _logger.info("[DataSentinel] Full backup cron is disabled in settings. Skipping.")
            return

        _logger.info("[DataSentinel] Triggering automated Full Instance Backup...")
        backup_rec = self.create({
            "backup_type": "full",
            "trigger_source": "cron",
            "include_db": config.get_param("data_sentinel.full_include_db", "True") == "True",
            "include_filestore": config.get_param("data_sentinel.full_include_filestore", "True") == "True",
            "include_addons": config.get_param("data_sentinel.full_include_addons", "True") == "True",
        })
        backup_rec._execute_backup()

    @api.model
    def _cron_timely_backup(self):
        """Automated hourly cron evaluating working hours window and interval."""
        config = self.env["ir.config_parameter"].sudo()
        if config.get_param("data_sentinel.timely_active", "True") != "True":
            return

        now = datetime.now()
        current_hour_decimal = now.hour + (now.minute / 60.0)

        start_hour = float(config.get_param("data_sentinel.timely_start_hour", 5.0))
        end_hour = float(config.get_param("data_sentinel.timely_end_hour", 20.0))
        interval_hours = int(config.get_param("data_sentinel.timely_interval_hours", 3))

        # Check if current time is within configured window (e.g. 5 AM to 8 PM)
        if not (start_hour <= current_hour_decimal <= end_hour):
            _logger.debug("[DataSentinel] Current hour (%.2f) outside timely window (%.2f - %.2f). Skipping.",
                          current_hour_decimal, start_hour, end_hour)
            return

        # Check when the last timely backup was run
        last_backup = self.search([
            ("backup_type", "in", ["timely", "full"]),
            ("state", "=", "success"),
        ], order="create_date desc", limit=1)

        if last_backup and last_backup.create_date:
            elapsed = datetime.now() - last_backup.create_date
            if elapsed < timedelta(hours=interval_hours, minutes=-10):  # 10 min grace
                _logger.debug("[DataSentinel] Last backup was %s ago (< %s hours). Skipping.", elapsed, interval_hours)
                return

        _logger.info("[DataSentinel] Triggering automated Timely Snapshot Backup...")
        backup_rec = self.create({
            "backup_type": "timely",
            "trigger_source": "cron",
            "include_db": config.get_param("data_sentinel.timely_include_db", "True") == "True",
            "include_filestore": config.get_param("data_sentinel.timely_include_filestore", "True") == "True",
            "include_addons": False,
        })
        backup_rec._execute_backup()

    @api.model
    def _run_retention_cleanup(self):
        """Execute automated retention purge on Nextcloud remote files and local server filesystem."""
        config = self.env["ir.config_parameter"].sudo()

        cloud_active = config.get_param("data_sentinel.cloud_retention_active", "True") == "True"
        full_days = int(config.get_param("data_sentinel.full_retention_days", 30))
        timely_days = int(config.get_param("data_sentinel.timely_retention_days", 7))

        local_active = config.get_param("data_sentinel.local_retention_active", "True") == "True"
        local_days = int(config.get_param("data_sentinel.local_retention_days", 7))
        local_max_count = int(config.get_param("data_sentinel.local_retention_count", 5))

        stats = {
            "cloud_deleted": 0,
            "local_deleted": 0,
            "records_unlinked": 0,
            "errors": [],
        }

        nc_url = config.get_param("data_sentinel.nextcloud_url")
        nc_user = config.get_param("data_sentinel.nextcloud_user")
        nc_pass = config.get_param("data_sentinel.nextcloud_password")
        verify_ssl = config.get_param("data_sentinel.verify_ssl") == "True"
        client = NextcloudWebDAVClient(nc_url, nc_user, nc_pass, verify_ssl=verify_ssl) if nc_url and nc_user else None

        # -------------------------------------------------------------
        # 1. CLOUD RETENTION PURGE
        # -------------------------------------------------------------
        if cloud_active and client:
            now = datetime.now()
            # A. Expired Full Backups
            if full_days > 0:
                cutoff_full = now - timedelta(days=full_days)
                expired_fulls = self.search([
                    ("backup_type", "=", "full"),
                    ("create_date", "<", cutoff_full),
                    ("nextcloud_uploaded", "=", True),
                ])
                for rec in expired_fulls:
                    if rec.nextcloud_path:
                        try:
                            if client.delete_file(rec.nextcloud_path):
                                stats["cloud_deleted"] += 1
                                rec.write({"nextcloud_uploaded": False, "nextcloud_path": False})
                                rec._append_log(f"Retention policy: deleted from Nextcloud (age > {full_days} days).")
                        except Exception as e:
                            stats["errors"].append(str(e))
                            _logger.warning("Retention error deleting cloud backup %s: %s", rec.name, str(e))

            # B. Expired Timely Backups
            if timely_days > 0:
                cutoff_timely = now - timedelta(days=timely_days)
                expired_timely = self.search([
                    ("backup_type", "=", "timely"),
                    ("create_date", "<", cutoff_timely),
                    ("nextcloud_uploaded", "=", True),
                ])
                for rec in expired_timely:
                    if rec.nextcloud_path:
                        try:
                            if client.delete_file(rec.nextcloud_path):
                                stats["cloud_deleted"] += 1
                                rec.write({"nextcloud_uploaded": False, "nextcloud_path": False})
                                rec._append_log(f"Retention policy: deleted from Nextcloud (age > {timely_days} days).")
                        except Exception as e:
                            stats["errors"].append(str(e))
                            _logger.warning("Retention error deleting cloud snapshot %s: %s", rec.name, str(e))

        # -------------------------------------------------------------
        # 2. LOCAL SERVER RETENTION PURGE
        # -------------------------------------------------------------
        if local_active:
            now = datetime.now()
            # A. Purge by Age (Days)
            if local_days > 0:
                cutoff_local = now - timedelta(days=local_days)
                expired_locals = self.search([
                    ("create_date", "<", cutoff_local),
                    ("local_path", "!=", False),
                ])
                for rec in expired_locals:
                    if rec.local_path and os.path.exists(rec.local_path):
                        try:
                            os.remove(rec.local_path)
                            stats["local_deleted"] += 1
                            rec.local_path = False
                            rec._append_log(f"Retention policy: deleted local file from server (age > {local_days} days).")
                        except Exception as e:
                            stats["errors"].append(str(e))
                            _logger.warning("Retention error deleting local file %s: %s", rec.local_path, str(e))
                    elif rec.local_path:
                        rec.local_path = False

            # B. Purge by Max Count (Keep only the most recent N local copies)
            if local_max_count > 0:
                all_with_local = self.search([
                    ("local_path", "!=", False),
                ], order="create_date desc, id desc")

                # Keep top local_max_count, purge the rest
                if len(all_with_local) > local_max_count:
                    excess_locals = all_with_local[local_max_count:]
                    for rec in excess_locals:
                        if rec.local_path and os.path.exists(rec.local_path):
                            try:
                                os.remove(rec.local_path)
                                stats["local_deleted"] += 1
                                rec.local_path = False
                                rec._append_log(f"Retention policy: deleted excess local file (exceeded max count {local_max_count}).")
                            except Exception as e:
                                stats["errors"].append(str(e))
                        elif rec.local_path:
                            rec.local_path = False

        # -------------------------------------------------------------
        # 3. CLEANUP ORPHANED / STALE DATABASE RECORDS
        # -------------------------------------------------------------
        stale_cutoff = datetime.now() - timedelta(days=max(full_days or 30, timely_days or 14, 30))
        stale_records = self.search([
            ("create_date", "<", stale_cutoff),
            ("nextcloud_uploaded", "=", False),
            ("local_path", "=", False),
        ])
        if stale_records:
            stats["records_unlinked"] = len(stale_records)
            stale_records.unlink()

        _logger.info("[DataSentinel] Retention Cleanup completed: %s", stats)
        return stats

    @api.model
    def _cron_cleanup_old_backups(self):
        """Clean up expired backups on Nextcloud and local filesystem."""
        return self._run_retention_cleanup()
