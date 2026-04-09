#!/usr/bin/env python3

"""
MTA Utility v3 Python - Cross-Platform (Windows/Linux/macOS)
This script replaces the bash version to ensure better Windows compatibility.
Features: 
- Fetches OpenShift manifests and docker image binaries.
- Zips the artifacts natively (no 'zip' CLI needed).
- Creates an MTA Application.
- Uploads the zip file directly to the application bucket.
- Triggers an Assessment with `binary: true` for jar analysis.
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
from requests.packages.urllib3.exceptions import InsecureRequestWarning

# Suppress insecure request warnings if they don't have valid SSL certs
requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

def setup_logging():
    log_file = "./mta_workflow_win_runtime.log"
    logging.basicConfig(
        level=logging.INFO,
        format="[%(levelname)s] %(asctime)s - %(message)s",
        handlers=[
            logging.FileHandler(log_file, mode='a', encoding='utf-8'),
            logging.StreamHandler(sys.stdout)
        ]
    )

def check_prereqs():
    logging.info("Running pre-flight checks...")
    tools = ["docker", "oc"]
    missing = []
    for tool in tools:
        if shutil.which(tool) is None:
            missing.append(tool)
    if missing:
        logging.error(f"Required command(s) '{', '.join(missing)}' could not be found in PATH.")
        sys.exit(1)

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
    logging.info("Testing MTA API connection and token validity...")
    headers = {"Authorization": f"Bearer {token}"}
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

def process_app_row(app_name, current_image, namespace, url, token, targets_csv, cleanup):
    app_name = "".join([c if c.isalnum() else "-" for c in app_name])
    extract_dir = f"./mta-assessment-{app_name}"
    app_zip = f"{app_name}.zip"
    container_id = None
    
    logging.info("===========================================")
    logging.info(f"Processing Row   : App={app_name} | Namespace={namespace}")
    logging.info(f"Image URL        : {current_image}")
    logging.info("===========================================")

    os.makedirs(extract_dir, exist_ok=True)

    # 1. Fetch OpenShift Manifests
    logging.info("1. Fetching OpenShift Resources...")
    fetch_oc_resources(namespace, extract_dir)

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

    # Check WORKDIR
    try:
        inspect_proc = subprocess.run(["docker", "inspect", "-f", "{{.Config.WorkingDir}}", current_image], capture_output=True, text=True)
        workdir = inspect_proc.stdout.strip()
        if workdir and workdir != "/":
            logging.info(f"Detected WORKDIR: {workdir}")
            dest_path = os.path.join(bin_dir, workdir.lstrip('/').replace('/', os.sep))
            os.makedirs(os.path.dirname(dest_path), exist_ok=True)
            cp_proc = subprocess.run(["docker", "cp", f"{container_id}:{workdir}", dest_path], capture_output=True)
            if cp_proc.returncode == 0:
                logging.info(f"Successfully extracted WORKDIR: {workdir}")
                extracted = True
    except Exception as e:
        logging.debug(f"WORKDIR extraction skipped or failed: {e}")

    # Inspect Entrypoint / Cmd for hints
    if not extracted:
        try:
            cmd_proc = subprocess.run(["docker", "inspect", "-f", '{{if .Config.Entrypoint}}{{join .Config.Entrypoint " "}}{{end}} {{if .Config.Cmd}}{{join .Config.Cmd " "}}{{end}}', current_image], capture_output=True, text=True)
            cmd_args = cmd_proc.stdout.strip().split()
            for arg in cmd_args:
                if any(arg.endswith(ext) for ext in [".jar", ".war", ".py", ".js", ".go"]) or arg.startswith("/"):
                    clean_arg = arg.strip(' "\'')
                    target_dir = os.path.dirname(clean_arg)
                    if target_dir not in ["/", "/bin", "/usr/bin", ""]:
                        dest_path = os.path.join(bin_dir, target_dir.lstrip('/').replace('/', os.sep))
                        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
                        cp_proc = subprocess.run(["docker", "cp", f"{container_id}:{target_dir}", dest_path], capture_output=True)
                        if cp_proc.returncode == 0:
                            logging.info(f"Successfully extracted application path: {target_dir}")
                            extracted = True
        except Exception as e:
            logging.debug(f"Entrypoint inspection failed: {e}")

    # Fallback paths
    if not extracted:
        app_paths = ["/app", "/opt/app-root/src", "/deployments", "/usr/src/app", "/usr/local/bin", "/var/www", "/opt"]
        for p in app_paths:
            dest_path = os.path.join(bin_dir, p.lstrip('/').replace('/', os.sep))
            os.makedirs(os.path.dirname(dest_path), exist_ok=True)
            cp_proc = subprocess.run(["docker", "cp", f"{container_id}:{p}", dest_path], capture_output=True)
            if cp_proc.returncode == 0:
                logging.info(f"Extracted common directory: {p}")
                extracted = True
                break
        
        # If still nothing, try single app.jar at root
        if not extracted:
            subprocess.run(["docker", "cp", f"{container_id}:/app.jar", bin_dir], capture_output=True)

    # 3. Zip the artifacts natively using Python
    logging.info(f"3. Creating zip archive {app_zip} for upload...")
    try:
        shutil.make_archive(app_name, 'zip', extract_dir)
    except Exception as e:
        logging.error(f"Failed to create ZIP archive: {e}")
        step_cleanup(app_name, current_image, container_id, extract_dir, app_zip, cleanup)
        return False

    # 4. MTA App Creation
    logging.info("4. Creating MTA Application...")
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
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
    try:
        with open(app_zip, "rb") as f:
            upload_resp = requests.post(
                f"{url}/hub/applications/{app_id}/bucket",
                headers={"Authorization": f"Bearer {token}"},
                files={"file": (app_zip, f, "application/zip")},
                verify=False
            )
        if upload_resp.status_code not in [200, 201, 204]:
            logging.error(f"File upload failed with HTTP code: {upload_resp.status_code}")
            step_cleanup(app_name, current_image, container_id, extract_dir, app_zip, cleanup)
            return False
        logging.info("File upload completed successfully.")
    except Exception as e:
        logging.error(f"Failed during file upload: {e}")
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

        submit_resp = requests.post(f"{url}/hub/taskgroups/{tg_id}/submit", headers={"Authorization": f"Bearer {token}"}, verify=False)
        submit_resp.raise_for_status()
        logging.info(f"Assessment triggered successfully for {app_name}!")
    except Exception as e:
        logging.error(f"Failed to start assessment or submit taskgroup: {e}")
        step_cleanup(app_name, current_image, container_id, extract_dir, app_zip, cleanup)
        return False

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
    parser.add_argument("-t", "--token", required=True, help="Bearer token for OpenShift/MTA authentication")
    parser.add_argument("-g", "--targets", default="cloud-readiness", help="Comma-separated list of assessment targets")
    parser.add_argument("-k", "--keep", action="store_true", help="Keep the extracted local artifacts")
    
    args = parser.parse_args()

    setup_logging()

    if not os.path.exists(args.csv):
        logging.error(f"CSV file '{args.csv}' does not exist.")
        sys.exit(1)

    url_clean = args.url.rstrip("/")

    check_prereqs()
    check_mta_api(url_clean, args.token)

    start_time = time.time()
    logging.info(f"Starting CSV batch processing from {args.csv}...")

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

                process_app_row(app_name, img_url, ns, url_clean, args.token, args.targets, not args.keep)

    duration = int(time.time() - start_time)
    logging.info("===========================================")
    logging.info(f"All tasks complete. Total execution time: {duration} seconds.")
    logging.info("===========================================")

if __name__ == "__main__":
    main()
