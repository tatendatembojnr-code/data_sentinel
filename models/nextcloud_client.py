import logging
import os
import requests
import urllib3

# Suppress self-signed certificate warnings for internal Nextcloud instances
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_logger = logging.getLogger(__name__)


class NextcloudWebDAVClient:
    """Robust WebDAV client for Nextcloud storage integration."""

    def __init__(self, base_url, username, password, verify_ssl=False, timeout=120):
        self.base_url = (base_url or "").rstrip("/")
        self.username = username or ""
        self.password = password or ""
        self.verify_ssl = verify_ssl
        self.timeout = timeout
        self.auth = (self.username, self.password) if self.username else None

    def _get_url(self, remote_path):
        """Construct full WebDAV URL from relative remote path."""
        clean_path = remote_path.strip("/")
        if not clean_path:
            return self.base_url
        return f"{self.base_url}/{clean_path}"

    def test_connection(self):
        """Test WebDAV connection and credentials using PROPFIND."""
        if not self.base_url or not self.username or not self.password:
            return False, "Nextcloud URL, Username, and Password are required."

        try:
            url = self.base_url
            headers = {"Depth": "0"}
            response = requests.request(
                "PROPFIND",
                url,
                auth=self.auth,
                headers=headers,
                verify=self.verify_ssl,
                timeout=15,
            )
            if response.status_code in [200, 207]:
                return True, "Successfully connected to Nextcloud WebDAV."
            elif response.status_code == 401:
                return False, "Authentication failed: Invalid Nextcloud username or password."
            elif response.status_code == 404:
                return False, f"Nextcloud WebDAV endpoint not found (404) at {url}."
            else:
                return False, f"Nextcloud returned HTTP status code {response.status_code}: {response.text[:200]}"
        except Exception as e:
            _logger.exception("Nextcloud connection test failed: %s", str(e))
            return False, f"Connection error: {str(e)}"

    def ensure_directory(self, remote_dir_path):
        """Recursively create remote directories on Nextcloud using MKCOL."""
        parts = [p for p in remote_dir_path.strip("/").split("/") if p]
        current_path = ""
        for part in parts:
            current_path = f"{current_path}/{part}" if current_path else part
            dir_url = self._get_url(current_path) + "/"
            try:
                # Check if directory exists
                check_resp = requests.request(
                    "PROPFIND",
                    dir_url,
                    auth=self.auth,
                    headers={"Depth": "0"},
                    verify=self.verify_ssl,
                    timeout=15,
                )
                if check_resp.status_code in [200, 207]:
                    continue  # Already exists
                # Create directory
                mkcol_resp = requests.request(
                    "MKCOL",
                    dir_url,
                    auth=self.auth,
                    verify=self.verify_ssl,
                    timeout=15,
                )
                if mkcol_resp.status_code not in [201, 405]:  # 201 Created, 405 Method Not Allowed (exists)
                    _logger.warning("MKCOL %s returned status %s", dir_url, mkcol_resp.status_code)
            except Exception as e:
                _logger.warning("Failed ensuring Nextcloud directory %s: %s", dir_url, str(e))
        return True

    def upload_file(self, local_file_path, remote_file_path):
        """Upload a local file to Nextcloud using streaming PUT."""
        if not os.path.exists(local_file_path):
            return False, f"Local file not found: {local_file_path}"

        # Ensure parent directory exists on Nextcloud
        remote_dir = os.path.dirname(remote_file_path.strip("/"))
        if remote_dir:
            self.ensure_directory(remote_dir)

        dest_url = self._get_url(remote_file_path)
        file_size = os.path.getsize(local_file_path)
        _logger.info("Uploading %s (%s bytes) to Nextcloud: %s", local_file_path, file_size, dest_url)

        try:
            with open(local_file_path, "rb") as file_data:
                headers = {
                    "Content-Type": "application/zip",
                    "Content-Length": str(file_size),
                }
                response = requests.put(
                    dest_url,
                    data=file_data,
                    auth=self.auth,
                    headers=headers,
                    verify=self.verify_ssl,
                    timeout=self.timeout,
                )

            if response.status_code in [200, 201, 204]:
                _logger.info("Upload successful for %s (HTTP %s)", dest_url, response.status_code)
                return True, f"Uploaded successfully (HTTP {response.status_code})"
            else:
                err_msg = f"Nextcloud upload failed with HTTP {response.status_code}: {response.text[:200]}"
                _logger.error(err_msg)
                return False, err_msg
        except Exception as e:
            err_msg = f"Exception during Nextcloud upload: {str(e)}"
            _logger.exception(err_msg)
            return False, err_msg

    def delete_file(self, remote_file_path):
        """Delete a file on Nextcloud."""
        dest_url = self._get_url(remote_file_path)
        try:
            response = requests.delete(
                dest_url,
                auth=self.auth,
                verify=self.verify_ssl,
                timeout=30,
            )
            return response.status_code in [200, 204, 404]
        except Exception as e:
            _logger.warning("Failed deleting remote Nextcloud file %s: %s", dest_url, str(e))
            return False
