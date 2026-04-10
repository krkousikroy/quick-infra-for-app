#!/usr/bin/env python3

"""
MTA Utility v3 Python - Cross-Platform (Windows/Linux/macOS)
This script replaces the bash version to ensure better Windows compatibility.
Features: 
- Optionally fetches OpenShift manifests and docker image binaries.
- Dynamically extracts all .jar, .war, and .ear files from containers.
- Zips the artifacts natively (no 'zip' CLI needed).
- Creates an MTA Application.
- Uploads the zip file directly to the application bucket.
- Triggers an Assessment with `binary: true` for jar analysis.
- Supports dry-run mode for testing.
- Token authentication is optional (not required if MTA authentication is disabled).
"""

import argparse
import csv
import logging
import os
import shutil
import ssl
import subprocess
import sys
import time
import json
import requests
import urllib3
import concurrent.futures
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.exceptions import InsecureRequestWarning
from urllib3.util.ssl_ import create_urllib3_context

# Suppress insecure request warnings if they don't have valid SSL certs
requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

def setup_logging(verbose=False):
    log_level = logging.DEBUG if verbose else logging.INFO
    log_file = "./mta_workflow_win_runtime.log"
    formatter = logging.Formatter("[%(levelname)s] %(asctime)s - %(message)s")
    
    logger = logging.getLogger()
    logger.setLevel(log_level)
    
    # Remove existing handlers to avoid duplicate logs
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
    
    file_handler = logging.FileHandler(log_file, mode='a', encoding='utf-8')
    file_handler.setFormatter(formatter)
    file_handler.setLevel(log_level)
    
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    console_handler.setLevel(log_level)
    
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)


class TLSAdapter(HTTPAdapter):
    def __init__(self, ssl_protocol=None, **kwargs):
        self.ssl_protocol = ssl_protocol or ssl.PROTOCOL_TLS_CLIENT
        super().__init__(**kwargs)

    def init_poolmanager(self, connections, maxsize, block=False, **pool_kwargs):
        ctx = create_urllib3_context(ssl_version=self.ssl_protocol)
        ctx.options |= ssl.OP_NO_SSLv2 | ssl.OP_NO_SSLv3
        pool_kwargs['ssl_context'] = ctx
        return super().init_poolmanager(connections, maxsize, block=block, **pool_kwargs)

    def proxy_manager_for(self, proxy, **proxy_kwargs):
        ctx = create_urllib3_context(ssl_version=self.ssl_protocol)
        ctx.options |= ssl.OP_NO_SSLv2 | ssl.OP_NO_SSLv3
        proxy_kwargs['ssl_context'] = ctx
        return super().proxy_manager_for(proxy, **proxy_kwargs)


def create_requests_session(verify=True, ca_bundle=None):
    session = requests.Session()
    session.trust_env = False
    if ca_bundle:
        session.verify = ca_bundle
    else:
        session.verify = verify
    if verify:
        adapter = TLSAdapter()
        session.mount('https://', adapter)
        session.mount('http://', adapter)
    return session


def check_prereqs(skip_oc=False):
    logging.info("Running pre-flight checks...")
    tools = ["docker"]
    if not skip_oc:
        tools.append("oc")
    missing = []
    for tool in tools:
        if shutil.which(tool) is None:
            missing.append(tool)
    if missing:
        logging.error(f"Required command(s) '{', '.join(missing)}' could not be found in PATH.")
        sys.exit(1)

    if not skip_oc:
        # Validate OpenShift Login
        try:
            subprocess.run(["oc", "whoami"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            logging.info("OpenShift CLI (oc) authentication verified.")
        except subprocess.CalledProcessError:
            logging.error("You do not appear to be logged into OpenShift. Please run 'oc login' first.")
            sys.exit(1)

    # Validate Docker daemon
    try:
        subprocess.run(["docker", "info"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        logging.info("Docker daemon is running.")
    except subprocess.CalledProcessError:
        logging.error("Docker daemon is not running. Please start Docker Desktop.")
        sys.exit(1)

def check_mta_api(url, token, session):
    logging.info("Testing MTA API connection...")
    headers = {}
    if token:
        headers['Authorization'] = f'Bearer {token}'
    try:
        response = session.get(f"{url}/hub/applications", headers=headers, timeout=10, verify=session.verify)
        if response.status_code == 200:
            logging.info("MTA API connection successful.")
        else:
            logging.error(f"MTA API connection failed. HTTP {response.status_code}: {response.text[:500]}")
            sys.exit(1)
    except requests.exceptions.Timeout:
        logging.error(f"MTA API connection timeout (10s). Check URL: {url}")
        sys.exit(1)
    except requests.exceptions.ConnectionError as e:
        logging.error(f"MTA API connection error: {e}")
        sys.exit(1)
    except Exception as e:
        logging.error(f"Unexpected MTA API error: {type(e).__name__}: {e}")
        sys.exit(1)

def fetch_oc_resources(namespace, out_dir):
    logging.info(f"Fetching OpenShift manifests for namespace: {namespace}")
    manifests_dir = os.path.join(out_dir, "manifests")
    os.makedirs(manifests_dir, exist_ok=True)
    
    resources = ["deployment", "service", "route", "ingress", "configmap", "secret", "statefulset", "daemonset"]
    for res in resources:
        try:
            # Check if resource exists
            check_proc = subprocess.run(["oc", "get", res, "-n", namespace], capture_output=True)
            if check_proc.returncode == 0:
                out_file = os.path.join(manifests_dir, f"{res}s.yaml")
                with open(out_file, "w") as f:
                    subprocess.run(["oc", "get", res, "-n", namespace, "-o", "yaml"], stdout=f, stderr=subprocess.DEVNULL)
                logging.info(f"  -> Exported {res} to manifests/{res}s.yaml")
        except Exception as e:
            logging.warning(f"Error fetching {res}: {e}")

def has_app_binaries(directory):
    for root, _, files in os.walk(directory):
        if any(f.endswith('.jar') or f.endswith('.war') or f.endswith('.py') or f.endswith('.js') or f.endswith('.go') for f in files):
            return True
    return False


def is_library_artifact(path):
    name = os.path.basename(path).lower()
    lower_path = path.lower()

    lib_dirs = [
        '/modules/', '/system/', '/var/lib/', '/usr/lib/', '/usr/share/',
        '/opt/jboss/wildfly/modules/', '/opt/jboss/eap/modules/',
        '/opt/jboss/wildfly/standalone/tmp/', '/opt/jboss/wildfly/standalone/data/'
    ]
    if any(segment in lower_path for segment in lib_dirs) and '/deployments/' not in lower_path:
        return True

    if name.endswith(('-sources.jar', '-javadoc.jar', '-tests.jar', '-test.jar')):
        return True

    library_tokens = [
        'junit', 'log4j', 'slf4j', 'hibernate', 'tomcat', 'wildfly', 'jboss',
        'xerces', 'logback', 'mariadb', 'mysql', 'postgresql', 'jdbc',
        'jaxb', 'activation', 'httpclient', 'httpcore', 'mockito', 'commons-'
    ]
    for token in library_tokens:
        if token.endswith('-'):
            if token in name:
                return True
        elif name.startswith(token) or name.endswith(token + '.jar') or name.endswith(token + '.war') or ('-' + token + '-') in name:
            return True

    return False


def score_application_artifact(path, app_name):
    name = os.path.basename(path).lower()
    lower_path = path.lower()
    score = 0

    if app_name and app_name.lower() in name:
        score += 40
    if name in ('app.jar', 'application.jar', 'root.war', 'root.jar', 'root.ear', 'main.jar'):
        score += 60
    if 'spring-boot' in name or name.endswith('-boot.jar'):
        score += 40
    if '/deployments/' in lower_path or '/opt/app' in lower_path or '/usr/local/tomcat/webapps/' in lower_path or '/opt/tomcat/webapps/' in lower_path:
        score += 30
    if lower_path.count('/') <= 3:
        score += 10
    if lower_path.endswith('.war') and '/modules/' not in lower_path and '/system/' not in lower_path:
        score += 10
    if is_library_artifact(path):
        score -= 50
    return score


def choose_application_artifacts(paths, app_name):
    scored = []
    for p in paths:
        score = score_application_artifact(p, app_name)
        scored.append((score, p))
        logging.debug(f"Artifact candidate: {p} score={score}")

    scored.sort(reverse=True)

    selected = [p for score, p in scored if score > 0]
    if selected:
        unique = []
        seen = set()
        for path in selected:
            base = os.path.basename(path)
            if base not in seen:
                unique.append(path)
                seen.add(base)
            if len(unique) == 2:
                break
        logging.debug(f"Selected artifacts: {unique}")
        return unique

    if scored:
        logging.debug(f"No high-scoring artifacts, falling back to best candidate: {scored[0][1]}")
        return [scored[0][1]]
    return []


def content_type_for_artifact(filename):
    if filename.lower().endswith(('.jar', '.war', '.ear')):
        return 'application/java-archive'
    return 'application/octet-stream'


def process_app_row(app_name, current_image, namespace, url, token, targets_csv, cleanup, skip_oc=False, session=None):
    app_name = "".join([c if c.isalnum() else "-" for c in app_name])
    extract_dir = f"./mta-assessment-{app_name}"
    app_zip = f"{app_name}.zip"
    container_id = None
    
    logging.info("===========================================")
    logging.info(f"Processing Row   : App={app_name} | Namespace={namespace}")
    logging.info(f"Image URL        : {current_image}")
    logging.info("===========================================")

    os.makedirs(extract_dir, exist_ok=True)

    # 1. Fetch OpenShift Manifests (optional)
    if not skip_oc:
        logging.info("1. Fetching OpenShift Resources...")
        fetch_oc_resources(namespace, extract_dir)
    else:
        logging.info("1. Skipping OpenShift Resources (as requested)")

    # 2. Extract Container Binaries
    logging.info(f"2. Fetching container image: {current_image}...")
    try:
        subprocess.run(["docker", "pull", current_image], check=True, stdout=subprocess.DEVNULL, timeout=600)
        logging.debug(f"Successfully pulled image {current_image}")
    except subprocess.TimeoutExpired:
        logging.error(f"Docker pull timeout (10 min) for {current_image}. Check network and image size.")
        step_cleanup(app_name, current_image, container_id, extract_dir, app_zip, cleanup)
        return False
    except subprocess.CalledProcessError as e:
        logging.error(f"Failed to pull image {current_image}: Exit code {e.returncode}")
        step_cleanup(app_name, current_image, container_id, extract_dir, app_zip, cleanup)
        return False

    logging.info("Creating temporary container...")
    try:
        create_proc = subprocess.run(["docker", "create", current_image], capture_output=True, text=True, check=True)
        container_id = create_proc.stdout.strip()
    except subprocess.CalledProcessError:
        logging.error(f"Failed to create Docker container from {current_image}. Skipping.")
        step_cleanup(app_name, current_image, container_id, extract_dir, app_zip, cleanup)
        return False

    artifact_dir = os.path.join(extract_dir, "artifacts")
    os.makedirs(artifact_dir, exist_ok=True)
    extracted = False

    logging.info("Starting container for binary extraction...")
    try:
        subprocess.run(["docker", "start", container_id], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        logging.debug(f"Started container {container_id}")
    except subprocess.TimeoutExpired:
        logging.error(f"Docker start timeout for container {container_id}.")
        step_cleanup(app_name, current_image, container_id, extract_dir, app_zip, cleanup)
        return False
    except subprocess.CalledProcessError as e:
        logging.error(f"Failed to start container {container_id}: Exit code {e.returncode}")
        step_cleanup(app_name, current_image, container_id, extract_dir, app_zip, cleanup)
        return False

    # Find all .jar, .war, and .ear files in the container
    logging.info("Searching for application binaries (.jar, .war, .ear) in the container...")
    jar_war_files = []
    find_shell = r"find / -type f \( -name '*.jar' -o -name '*.war' -o -name '*.ear' \) -not -path '/proc/*' -not -path '/sys/*' -not -path '/dev/*' -not -path '/tmp/*' -not -path '/var/tmp/*' 2>/dev/null"
    try:
        find_proc = subprocess.run([
            "docker", "exec", container_id, "sh", "-c", find_shell
        ], capture_output=True, text=True, timeout=120)
        if find_proc.returncode == 0:
            jar_war_files = [line.strip() for line in find_proc.stdout.splitlines() if line.strip()]
            logging.info(f"Found {len(jar_war_files)} candidate binaries.")
        else:
            logging.warning(f"Container find returned code {find_proc.returncode}; stderr={find_proc.stderr.strip()}")
    except subprocess.TimeoutExpired:
        logging.warning("Find command timed out.")
    except Exception as e:
        logging.warning(f"Error during binary search: {e}")

    if not jar_war_files:
        logging.info("Trying fallback search in known application directories...")
        known_roots = [
            '/opt/app', '/deployments', '/app', '/opt/app-root/src', '/usr/local/tomcat/webapps',
            '/opt/tomcat/webapps', '/opt/jboss/wildfly/standalone/deployments', '/usr/src/app', '/home'
        ]
        for root in known_roots:
            try:
                find_proc = subprocess.run([
                    "docker", "exec", container_id, "sh", "-c",
                    rf"find {root} -type f \( -name '*.jar' -o -name '*.war' -o -name '*.ear' \) 2>/dev/null"
                ], capture_output=True, text=True, timeout=60)
                if find_proc.returncode == 0:
                    extra = [line.strip() for line in find_proc.stdout.splitlines() if line.strip()]
                    if extra:
                        logging.info(f"Found {len(extra)} binaries under {root}")
                        jar_war_files.extend(extra)
                else:
                    logging.debug(f"Fallback find under {root} returned {find_proc.returncode}")
            except Exception:
                logging.debug(f"Fallback search failed for {root}")
        jar_war_files = list(dict.fromkeys(jar_war_files))
        logging.info(f"Fallback found {len(jar_war_files)} candidate binaries.")

    selected_files = choose_application_artifacts(jar_war_files, app_name)
    if not selected_files and jar_war_files:
        selected_files = [jar_war_files[0]]
        logging.warning("No clear main artifact identified; falling back to the first discovered binary.")

    if not selected_files:
        logging.error("No application jar/war artifacts found or selected for upload.")
        step_cleanup(app_name, current_image, container_id, extract_dir, app_zip, cleanup)
        return False

    for file_path in selected_files:
        rel_path = os.path.basename(file_path)
        dest_file = os.path.join(artifact_dir, rel_path)
        logging.info(f"Copying selected artifact {file_path} to local path {dest_file}")
        os.makedirs(os.path.dirname(dest_file), exist_ok=True)
        cp_proc = subprocess.run(["docker", "cp", f"{container_id}:{file_path}", dest_file], capture_output=True)
        if cp_proc.returncode != 0:
            logging.error(f"Failed to copy selected artifact {file_path} from container.")
            step_cleanup(app_name, current_image, container_id, extract_dir, app_zip, cleanup)
            return False
        extracted = True

    # Stop the container
    try:
        subprocess.run(["docker", "stop", container_id], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except subprocess.CalledProcessError:
        logging.warning(f"Failed to stop container {container_id}.")

    if not extracted:
        logging.error("No application artifacts were extracted for upload.")
        step_cleanup(app_name, current_image, container_id, extract_dir, app_zip, cleanup)
        return False

    logging.info("Application artifact extraction completed.")

    artifact_files = [os.path.join(artifact_dir, os.path.basename(p)) for p in selected_files]
    artifact_name = os.path.basename(artifact_files[0])

    # 4. MTA App Creation
    logging.info("4. Creating MTA Application...")
    headers = {
        "Content-Type": "application/json"
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
        
    description = f"Docker Image: {current_image} | NS: {namespace}"
    if len(description) > 1024:
        description = description[:1021] + "..."
    
    app_payload = {
        "name": app_name,
        "description": description
    }
    try:
        app_resp = session.post(f"{url}/hub/applications", headers=headers, json=app_payload, timeout=30, verify=session.verify)
        app_resp.raise_for_status()
        response_data = app_resp.json()
        app_id = response_data.get("id")
        if not app_id:
            logging.error(f"Application creation response missing 'id' field: {response_data}")
            step_cleanup(app_name, current_image, container_id, extract_dir, app_zip, cleanup)
            return False
        logging.info(f"Application successfully created in MTA with ID: {app_id}")
    except requests.exceptions.RequestException as e:
        logging.error(f"Failed to create application: HTTP {e.response.status_code if hasattr(e, 'response') else 'error'}: {e}")
        step_cleanup(app_name, current_image, container_id, extract_dir, app_zip, cleanup)
        return False
    except (ValueError, KeyError) as e:
        logging.error(f"Failed to parse application response: {e}")
        step_cleanup(app_name, current_image, container_id, extract_dir, app_zip, cleanup)
        return False

    # 5. Assessment taskgroup configuration
    # Note: binary=True forces MTA to evaluate the JARs rather than treating it as a generic source bundle
    logging.info("5. Creating Assessment TaskGroup...")
    targets_list = [t.strip() for t in targets_csv.split(",") if t.strip()]
    
    assessment_data = {
        "name": f"assessment-{app_name}",
        "state": "Ready",
        "data": {
            "mode": {
                "artifact": artifact_name,
                "binary": True,
                "withDeps": False,
                "diva": False
            },
            "targets": targets_list,
            "sources": [],
            "scope": { "withKnown": False },
            "rules": { "path": "", "tags": [] }
        },
        "tasks": [
            {
                "name": app_name,
                "application": {"id": app_id}
            }
        ]
    }

    tg_id = None
    try:
        tg_resp = session.post(f"{url}/hub/taskgroups", headers=headers, json=assessment_data, timeout=30, verify=session.verify)
        tg_resp.raise_for_status()
        tg_data = tg_resp.json()
        tg_id = tg_data.get("id")
        if not tg_id:
            logging.error(f"TaskGroup creation response missing 'id' field: {tg_data}")
            step_cleanup(app_name, current_image, container_id, extract_dir, app_zip, cleanup)
            return False
        logging.info(f"Assessment TaskGroup created successfully: ID={tg_id}")
    except requests.exceptions.RequestException as e:
        logging.error(f"Failed to create taskgroup: HTTP error: {e}")
        step_cleanup(app_name, current_image, container_id, extract_dir, app_zip, cleanup)
        return False
    except (ValueError, KeyError) as e:
        logging.error(f"Failed to parse taskgroup response: {e}")
        step_cleanup(app_name, current_image, container_id, extract_dir, app_zip, cleanup)
        return False

    # 6. Upload selected JAR/WAR artifacts to the taskgroup bucket
    logging.info("6. Uploading selected application artifacts to taskgroup bucket...")
    upload_headers = {}
    if token:
        upload_headers["Authorization"] = f"Bearer {token}"

    bucket_url = f"{url}/hub/taskgroups/{tg_id}/bucket/binary"
    max_retries = 3

    for local_file in artifact_files:
        file_name = os.path.basename(local_file)
        target_url = f"{bucket_url}/{file_name}"
        logging.info(f"Uploading artifact {file_name} to {target_url}")
        file_uploaded = False

        for attempt in range(max_retries):
            try:
                file_size = os.path.getsize(local_file)
                logging.debug(f"Uploading {file_name} ({file_size} bytes) attempt {attempt + 1}/{max_retries}")
                
                with open(local_file, "rb") as f:
                    put_headers = upload_headers.copy()
                    put_headers["Content-Type"] = content_type_for_artifact(file_name)
                    put_resp = session.put(
                        target_url,
                        headers=put_headers,
                        data=f,
                        timeout=300,
                        verify=session.verify
                    )

                if put_resp.status_code in [200, 201, 204]:
                    logging.info(f"Artifact {file_name} ({file_size} bytes) uploaded successfully.")
                    file_uploaded = True
                    break
                
                error_detail = put_resp.text[:200] if put_resp.text else "No response body"
                logging.debug(f"PUT returned {put_resp.status_code}: {error_detail}")

                if attempt < max_retries - 1:
                    wait_time = 2 ** attempt
                    logging.info(f"Retrying upload in {wait_time} seconds...")
                    time.sleep(wait_time)
                    continue

                if attempt == max_retries - 1:
                    logging.info("Attempting multipart POST fallback upload...")
                    with open(local_file, "rb") as f:
                        post_resp = session.post(
                            bucket_url,
                            headers=upload_headers,
                            files={"file": (file_name, f, "application/java-archive")},
                            timeout=300,
                            verify=session.verify
                        )
                    if post_resp.status_code in [200, 201, 204]:
                        logging.info(f"Artifact {file_name} uploaded successfully via multipart POST.")
                        file_uploaded = True
                    else:
                        logging.debug(f"POST failed: {post_resp.text[:200]}")
                break

            except requests.exceptions.Timeout:
                logging.warning(f"Upload timeout for {file_name} attempt {attempt + 1}/{max_retries}")
                if attempt == max_retries - 1:
                    logging.error("Max retries reached for upload (all timed out).")
                    step_cleanup(app_name, current_image, container_id, extract_dir, app_zip, cleanup)
                    return False
                wait_time = 2 ** attempt
                logging.info(f"Retrying in {wait_time} seconds...")
                time.sleep(wait_time)
            except Exception as e:
                logging.warning(f"Upload error for {file_name} attempt {attempt + 1}/{max_retries}: {type(e).__name__}")
                if attempt == max_retries - 1:
                    logging.error(f"Max retries reached. Upload failed: {e}")
                    step_cleanup(app_name, current_image, container_id, extract_dir, app_zip, cleanup)
                    return False
                wait_time = 2 ** attempt
                logging.debug(f"Retrying in {wait_time} seconds...")
                time.sleep(wait_time)

        if not file_uploaded:
            logging.error(f"Failed to upload artifact {file_name} after all attempts.")
            step_cleanup(app_name, current_image, container_id, extract_dir, app_zip, cleanup)
            return False

    # 7. Submit taskgroup for execution
    logging.info("7. Submitting Assessment for execution...")
    try:
        submit_headers = {}
        if token:
            submit_headers["Authorization"] = f"Bearer {token}"
            
        submit_resp = session.post(f"{url}/hub/taskgroups/{tg_id}/submit", headers=submit_headers, timeout=30, verify=session.verify)
        submit_resp.raise_for_status()
        logging.info(f"Assessment triggered successfully for {app_name}!")
        logging.info(f"TaskGroup {tg_id} is now executing. Monitor progress in MTA UI.")
    except requests.exceptions.RequestException as e:
        logging.error(f"Failed to submit taskgroup: {e}")
        step_cleanup(app_name, current_image, container_id, extract_dir, app_zip, cleanup)
        return False

    logging.info(f"Processing completed successfully for {app_name}")
    step_cleanup(app_name, current_image, container_id, extract_dir, app_zip, cleanup)
    return True

def step_cleanup(app_name, current_image, container_id, extract_dir, app_zip, cleanup):
    if cleanup:
        logging.info(f"[CLEANUP] Removing temporary artifacts for {app_name}...")
        
        if container_id:
            try:
                subprocess.run(
                    ["docker", "rm", "-f", container_id],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=30
                )
                logging.debug(f"Removed Docker container {container_id}")
            except Exception as e:
                logging.warning(f"Failed to remove container {container_id}: {e}")
        
        if current_image:
            logging.debug(f"[CLEANUP] Removing image {current_image} from Docker...")
            try:
                subprocess.run(
                    ["docker", "rmi", "-f", current_image],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=60
                )
                logging.debug(f"Removed Docker image {current_image}")
            except Exception as e:
                logging.warning(f"Failed to remove image {current_image}: {e}")
        
        # Clean up extracted directories
        if os.path.exists(extract_dir):
            try:
                shutil.rmtree(extract_dir, ignore_errors=True)
                logging.debug(f"Removed extraction directory: {extract_dir}")
            except Exception as e:
                logging.warning(f"Failed to remove directory {extract_dir}: {e}")
        
        # Clean up zip file
        if os.path.exists(app_zip):
            try:
                os.remove(app_zip)
                logging.debug(f"Removed zip file: {app_zip}")
            except OSError as e:
                logging.warning(f"Failed to remove zip file {app_zip}: {e}")
    else:
        logging.info(f"[INFO] Keeping artifacts for {app_name} (--keep flag set). Manual cleanup may be needed.")

def main():
    parser = argparse.ArgumentParser(description="MTA Utility v3 Python - Windows/Cross-Platform")
    parser.add_argument("-c", "--csv", required=True, help="CSV file containing: app_name,image_url,namespace")
    parser.add_argument("-u", "--url", required=True, help="MTA Hub URL (e.g., https://mta...apps.cluster.com)")
    parser.add_argument("-t", "--token", required=False, default=None, help="Bearer token for MTA authentication (optional if authentication is disabled in your MTA setup)")
    parser.add_argument("-g", "--targets", default="cloud-readiness", help="Comma-separated list of assessment targets")
    parser.add_argument("-k", "--keep", action="store_true", help="Keep the extracted local artifacts")
    parser.add_argument("--skip-oc", action="store_true", help="Skip fetching OpenShift manifests")
    parser.add_argument("--insecure", action="store_true", help="Disable TLS certificate verification for MTA API connections")
    parser.add_argument("--ca-bundle", help="Path to a CA bundle file to trust for TLS certificate verification")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose logging (debug level)")
    parser.add_argument("--dry-run", action="store_true", help="Dry run mode: simulate operations without executing them")
    
    args = parser.parse_args()

    setup_logging(verbose=args.verbose)

    if not os.path.exists(args.csv):
        logging.error(f"CSV file '{args.csv}' does not exist.")
        sys.exit(1)

    url_clean = args.url.rstrip("/")

    verify = False if args.insecure else True
    if args.ca_bundle:
        verify = args.ca_bundle
    session = create_requests_session(verify=verify, ca_bundle=args.ca_bundle)

    if args.dry_run:
        logging.info("DRY RUN MODE: Simulating operations without executing them.")
        logging.info(f"Would check prerequisites and MTA API connection to {url_clean}")
        logging.info(f"Would process CSV file {args.csv}")
        logging.info("Dry run complete. Exiting.")
        sys.exit(0)

    check_prereqs(skip_oc=args.skip_oc)
    check_mta_api(url_clean, args.token, session)

    start_time = time.time()
    logging.info(f"Starting CSV batch processing from {args.csv}...")

    tasks = []
    row_number = 0
    try:
        with open(args.csv, "r", encoding="utf-8") as f:
            reader = csv.reader(f)
            for row_number, row in enumerate(reader, start=1):
                # Skip empty rows and comments
                if not row or all(not cell.strip() for cell in row):
                    continue
                if row[0].strip().startswith("#"):
                    logging.debug(f"Skipping comment row {row_number}")
                    continue
                
                # Skip header row
                if row_number == 1 and "app_name" in row[0].lower():
                    logging.debug("Skipping CSV header row")
                    continue
                
                if len(row) < 3:
                    logging.warning(f"CSV row {row_number}: Insufficient columns (need 3, got {len(row)}). Skipping.")
                    continue
                
                app_name = row[0].strip()
                img_url = row[1].strip()
                ns = row[2].strip()
                
                # Validate non-empty
                if not app_name or not img_url or not ns:
                    logging.warning(f"CSV row {row_number}: Missing required fields. Skipping.")
                    continue
                
                # Validate app_name
                if len(app_name) > 255:
                    logging.warning(f"CSV row {row_number}: app_name exceeds 255 characters. Truncating.")
                    app_name = app_name[:255]

                tasks.append((app_name, img_url, ns))
    except UnicodeDecodeError as e:
        logging.error(f"CSV file encoding error at row {row_number}: {e}. Ensure file is UTF-8 encoded.")
        sys.exit(1)
    except Exception as e:
        logging.error(f"Error reading CSV file: {e}")
        sys.exit(1)

    if not tasks:
        logging.warning("No valid application rows found in CSV.")
        sys.exit(0)

    success_count = 0
    failure_count = 0

    # Max concurrent workers (throttle so it doesn't overwhelm Docker Daemon or memory)
    max_workers = min(3, len(tasks))
    logging.info(f"Starting {max_workers} parallel workers for {len(tasks)} applications...")

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_app = {
            executor.submit(process_app_row, app_name, img_url, ns, url_clean, args.token, args.targets, not args.keep, args.skip_oc, session): app_name 
            for (app_name, img_url, ns) in tasks
        }
        
        for future in concurrent.futures.as_completed(future_to_app):
            app_name = future_to_app[future]
            try:
                result = future.result()
                if result:
                    success_count += 1
                else:
                    failure_count += 1
            except Exception as e:
                logging.error(f"Critical exception processing {app_name}: {e}")
                failure_count += 1

    duration = int(time.time() - start_time)
    logging.info("="*50)
    logging.info(f"Batch processing complete in {duration} seconds.")
    logging.info(f"Results: {success_count} succeeded, {failure_count} failed out of {len(tasks)} total.")
    if failure_count > 0:
        logging.warning(f"Some applications failed processing. Review logs for details.")
    logging.info("="*50)
    
    # Exit with proper code
    sys.exit(0 if failure_count == 0 else 1)

if __name__ == "__main__":
    main()
