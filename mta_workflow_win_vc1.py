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
import subprocess
import sys
import time
import json
import requests
import concurrent.futures
from requests.packages.urllib3.exceptions import InsecureRequestWarning

# Suppress insecure request warnings if they don't have valid SSL certs
requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

def setup_logging(verbose=False):
    log_level = logging.DEBUG if verbose else logging.INFO
    log_file = "./mta_workflow_win_runtime.log"
    logging.basicConfig(
        level=log_level,
        format="[%(levelname)s] %(asctime)s - %(message)s",
        handlers=[
            logging.FileHandler(log_file, mode='a', encoding='utf-8'),
            logging.StreamHandler(sys.stdout)
        ]
    )

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

def check_mta_api(url, token):
    logging.info("Testing MTA API connection...")
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        response = requests.get(f"{url}/hub/applications", headers=headers, verify=False, timeout=10)
        if response.status_code == 200:
            logging.info("MTA API connection successful.")
        else:
            logging.error(f"MTA API connection failed. HTTP Code: {response.status_code}")
            sys.exit(1)
    except Exception as e:
        logging.error(f"MTA API connection error: {e}")
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

def process_app_row(app_name, current_image, namespace, url, token, targets_csv, cleanup, skip_oc=False):
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
        subprocess.run(["docker", "pull", current_image], check=True, stdout=subprocess.DEVNULL)
    except subprocess.CalledProcessError:
        logging.error(f"Failed to pull image {current_image} via Docker. Skipping.")
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

    bin_dir = os.path.join(extract_dir, "source")
    os.makedirs(bin_dir, exist_ok=True)
    extracted = False

    # Start the container to allow exec
    logging.info("Starting container for binary extraction...")
    try:
        subprocess.run(["docker", "start", container_id], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except subprocess.CalledProcessError:
        logging.error(f"Failed to start container {container_id}. Skipping.")
        step_cleanup(app_name, current_image, container_id, extract_dir, app_zip, cleanup)
        return False

    # Find all .jar, .war, and .ear files in the container
    logging.info("Searching for application binaries (.jar, .war, .ear) in the container...")
    try:
        find_proc = subprocess.run(["docker", "exec", container_id, "find", "/", "-type", "f", "(", "-name", "*.jar", "-o", "-name", "*.war", "-o", "-name", "*.ear", ")", "-not", "-path", "/proc/*", "-not", "-path", "/sys/*", "-not", "-path", "/dev/*", "-not", "-path", "/tmp/*", "-not", "-path", "/var/tmp/*"], capture_output=True, text=True, timeout=60)
        if find_proc.returncode == 0:
            jar_war_files = [line.strip() for line in find_proc.stdout.split('\n') if line.strip()]
            logging.info(f"Found {len(jar_war_files)} application binaries.")
            extracted_count = 0
            for file_path in jar_war_files:
                # Copy each file to bin_dir, preserving relative path structure
                rel_path = file_path.lstrip('/')
                dest_file = os.path.join(bin_dir, rel_path.replace('/', os.sep))
                os.makedirs(os.path.dirname(dest_file), exist_ok=True)
                cp_proc = subprocess.run(["docker", "cp", f"{container_id}:{file_path}", dest_file], capture_output=True)
                if cp_proc.returncode == 0:
                    logging.debug(f"Copied {file_path} to {rel_path}")
                    extracted_count += 1
                else:
                    logging.debug(f"Failed to copy {file_path}")
            if extracted_count > 0:
                extracted = True
                logging.info(f"Successfully extracted {extracted_count} application binaries.")
            else:
                logging.warning("No binaries were successfully copied.")
        else:
            logging.warning("Find command failed or timed out.")
    except subprocess.TimeoutExpired:
        logging.warning("Find command timed out.")
    except Exception as e:
        logging.warning(f"Error during binary search: {e}")

    # Stop the container
    try:
        subprocess.run(["docker", "stop", container_id], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except subprocess.CalledProcessError:
        logging.warning(f"Failed to stop container {container_id}.")

    if not extracted:
        logging.warning("No application binaries found in the container.")
    else:
        logging.info("Application binaries extraction completed.")

    # 3. Zip the artifacts natively using Python
    logging.info(f"3. Creating zip archive {app_zip} for upload...")
    try:
        shutil.make_archive(app_name, 'zip', extract_dir)
        zip_size = os.path.getsize(app_zip)
        logging.info(f"ZIP archive created successfully. Size: {zip_size} bytes")
        
        # Validate zip contents
        import zipfile
        with zipfile.ZipFile(app_zip, 'r') as zf:
            file_list = zf.namelist()
            if not file_list:
                logging.error("ZIP archive is empty. No artifacts to upload.")
                step_cleanup(app_name, current_image, container_id, extract_dir, app_zip, cleanup)
                return False
            logging.info(f"ZIP contains {len(file_list)} files/directories.")
    except Exception as e:
        logging.error(f"Failed to create or validate ZIP archive: {e}")
        step_cleanup(app_name, current_image, container_id, extract_dir, app_zip, cleanup)
        return False

    # 4. MTA App Creation
    logging.info("4. Creating MTA Application...")
    headers = {
        "Content-Type": "application/json"
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
        
    app_payload = {
        "name": app_name,
        "description": f"Docker Image: {current_image} | NS: {namespace}"
    }
    try:
        app_resp = requests.post(f"{url}/hub/applications", headers=headers, json=app_payload, verify=False)
        app_resp.raise_for_status()
        app_id = app_resp.json().get("id")
        logging.info(f"Application successfully created in MTA with ID: {app_id}")
    except Exception as e:
        logging.error(f"Failed to create application: {e}")
        step_cleanup(app_name, current_image, container_id, extract_dir, app_zip, cleanup)
        return False

    # Upload zip to bucket
    logging.info("Uploading compiled zip file (Manifests + Binaries) to application bucket...")
    upload_headers = {}
    if token:
        upload_headers["Authorization"] = f"Bearer {token}"
    
    max_retries = 3
    for attempt in range(max_retries):
        try:
            with open(app_zip, "rb") as f:
                upload_resp = requests.post(
                    f"{url}/hub/applications/{app_id}/bucket/",
                    headers=upload_headers,
                    files={"file": (app_zip, f, "application/zip")},
                    verify=False,
                    timeout=300  # 5 minutes timeout
                )
            if upload_resp.status_code in [200, 201, 204]:
                logging.info("File upload completed successfully.")
                break
            elif upload_resp.status_code == 400:
                logging.error(f"File upload failed with HTTP 400 (Bad Request) on attempt {attempt + 1}/{max_retries}")
                logging.error(f"Response body: {upload_resp.text}")
                if attempt < max_retries - 1:
                    wait_time = 2 ** attempt  # Exponential backoff
                    logging.info(f"Retrying upload in {wait_time} seconds...")
                    time.sleep(wait_time)
                else:
                    logging.error("Max retries reached. Upload failed.")
                    step_cleanup(app_name, current_image, container_id, extract_dir, app_zip, cleanup)
                    return False
            else:
                logging.error(f"File upload failed with HTTP code: {upload_resp.status_code} on attempt {attempt + 1}/{max_retries}")
                logging.error(f"Response body: {upload_resp.text}")
                if attempt < max_retries - 1:
                    wait_time = 2 ** attempt
                    logging.info(f"Retrying upload in {wait_time} seconds...")
                    time.sleep(wait_time)
                else:
                    logging.error("Max retries reached. Upload failed.")
                    step_cleanup(app_name, current_image, container_id, extract_dir, app_zip, cleanup)
                    return False
        except Exception as e:
            logging.error(f"Failed during file upload attempt {attempt + 1}/{max_retries}: {e}")
            if attempt < max_retries - 1:
                wait_time = 2 ** attempt
                logging.info(f"Retrying upload in {wait_time} seconds...")
                time.sleep(wait_time)
            else:
                logging.error("Max retries reached. Upload failed.")
                step_cleanup(app_name, current_image, container_id, extract_dir, app_zip, cleanup)
                return False

    # 5. Assessment taskgroup configuration
    # Note: binary=True forces MTA to evaluate the JARs rather than treating it as a generic source bundle
    logging.info("5. Running Assessment...")
    targets_list = [t.strip() for t in targets_csv.split(",")]
    
    assessment_data = {
        "name": f"assessment-{app_name}",
        "state": "Ready",
        "data": {
            "mode": {
                "artifact": app_zip,
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

    try:
        tg_resp = requests.post(f"{url}/hub/taskgroups", headers=headers, json=assessment_data, verify=False)
        tg_resp.raise_for_status()
        tg_id = tg_resp.json().get("id")
        logging.info(f"Assessment TaskGroup created successfully: ID={tg_id}")

        submit_headers = {}
        if token:
            submit_headers["Authorization"] = f"Bearer {token}"
            
        submit_resp = requests.post(f"{url}/hub/taskgroups/{tg_id}/submit", headers=submit_headers, verify=False)
        submit_resp.raise_for_status()
        logging.info(f"Assessment triggered successfully for {app_name}!")
    except Exception as e:
        logging.error(f"Failed to start assessment or submit taskgroup: {e}")
        step_cleanup(app_name, current_image, container_id, extract_dir, app_zip, cleanup)
        return False

    logging.info(f"Processing completed successfully for {app_name}")
    step_cleanup(app_name, current_image, container_id, extract_dir, app_zip, cleanup)
    return True

def step_cleanup(app_name, current_image, container_id, extract_dir, app_zip, cleanup):
    if cleanup:
        logging.info(f"[CLEANUP] Removing artifacts for {app_name}...")
        if container_id:
            subprocess.run(["docker", "rm", "-f", container_id], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if current_image:
            logging.info(f"[CLEANUP] Removing image {current_image} from Docker...")
            subprocess.run(["docker", "rmi", "-f", current_image], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        
        # Windows-friendly folder removal
        if os.path.exists(extract_dir):
            shutil.rmtree(extract_dir, ignore_errors=True)
        if os.path.exists(app_zip):
            try:
                os.remove(app_zip)
            except OSError:
                pass

def main():
    parser = argparse.ArgumentParser(description="MTA Utility v3 Python - Windows/Cross-Platform")
    parser.add_argument("-c", "--csv", required=True, help="CSV file containing: app_name,image_url,namespace")
    parser.add_argument("-u", "--url", required=True, help="MTA Hub URL (e.g., https://mta...apps.cluster.com)")
    parser.add_argument("-t", "--token", required=False, default=None, help="Bearer token for MTA authentication (optional if authentication is disabled in your MTA setup)")
    parser.add_argument("-g", "--targets", default="cloud-readiness", help="Comma-separated list of assessment targets")
    parser.add_argument("-k", "--keep", action="store_true", help="Keep the extracted local artifacts")
    parser.add_argument("--skip-oc", action="store_true", help="Skip fetching OpenShift manifests")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose logging (debug level)")
    parser.add_argument("--dry-run", action="store_true", help="Dry run mode: simulate operations without executing them")
    
    args = parser.parse_args()

    setup_logging(verbose=args.verbose)

    if not os.path.exists(args.csv):
        logging.error(f"CSV file '{args.csv}' does not exist.")
        sys.exit(1)

    url_clean = args.url.rstrip("/")

    if args.dry_run:
        logging.info("DRY RUN MODE: Simulating operations without executing them.")
        logging.info(f"Would check prerequisites and MTA API connection to {url_clean}")
        logging.info(f"Would process CSV file {args.csv}")
        logging.info("Dry run complete. Exiting.")
        sys.exit(0)

    check_prereqs(skip_oc=args.skip_oc)
    check_mta_api(url_clean, args.token)

    start_time = time.time()
    logging.info(f"Starting CSV batch processing from {args.csv}...")

    tasks = []
    with open(args.csv, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        for row in reader:
            if not row or "".join(row).strip() == "" or row[0].startswith("#"):
                continue
            
            if len(row) >= 3:
                app_name = row[0].strip()
                img_url = row[1].strip()
                ns = row[2].strip()
                
                if not app_name or not img_url or not ns or "app_name" in app_name:
                    continue

                tasks.append((app_name, img_url, ns))

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
            executor.submit(process_app_row, app_name, img_url, ns, url_clean, args.token, args.targets, not args.keep, args.skip_oc): app_name 
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
    logging.info("===========================================")
    logging.info(f"All tasks complete. Total execution time: {duration} seconds.")
    logging.info(f"Summary: {success_count} Succeeded, {failure_count} Failed.")
    logging.info("===========================================")

if __name__ == "__main__":
    main()
