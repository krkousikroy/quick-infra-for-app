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
import tarfile
import concurrent.futures
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
            logging.error("You do not appear to be logged into OpenShift. Please run 'oc login' first, or use --skip-oc to skip this.")
            sys.exit(1)

    # Validate Docker daemon
    try:
        subprocess.run(["docker", "info"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
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

def process_app_row(app_name, current_image, namespace, url, token, targets_csv, cleanup, skip_oc):
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
    if not skip_oc and namespace:
        logging.info(f"1. Fetching OpenShift Resources for namespace: {namespace}...")
        fetch_oc_resources(namespace, extract_dir)
    else:
        logging.info("1. Skipping OpenShift Resource extraction as requested (--skip-oc) or no namespace provided.")

    # 2. Extract Container Binaries
    logging.info(f"2. Preparing container image: {current_image}...")
    
    # We use docker create directly. Docker will use the local disk cache if available 
    # (great for offline image scanning), otherwise it automatically pulls it from the registry.
    logging.info(f"Resolving image and creating temporary container...")
    try:
        create_proc = subprocess.run(["docker", "create", current_image], capture_output=True, text=True, check=True)
        container_id = create_proc.stdout.strip()
    except subprocess.CalledProcessError:
        logging.error(f"Failed to find or pull Docker container from {current_image}. Skipping.")
        step_cleanup(app_name, current_image, container_id, extract_dir, cleanup)
        return False

    bin_dir = os.path.join(extract_dir, "source")
    os.makedirs(bin_dir, exist_ok=True)
    extracted = False
    main_artifacts = []

    logging.info(f"Dynamically scanning container filesystem for {current_image} (ignoring generic framework libraries)...")
    
    # Aggressively ignore base framework libraries and inner dependency folders to isolate ONLY the main application artifact!
    ignore_substrings = [
        "/usr/lib/jvm/", "/usr/java/", "jre/lib/", "jdk/lib/", 
        "/.m2/", "/.gradle/", "/var/cache/", "/usr/share/", "/usr/lib64/",
        "/modules/system/", "/wlp/lib/", "/tomcat/lib/", "apache-tomcat/lib/",
        "/lib/", "/libs/", "WEB-INF/lib/", "BOOT-INF/lib/", "/dependencies/",
        "/node_modules/", "/venv/", "/.npm/"
    ]
    
    try:
        # Stream the entirely flattened filesystem image from the docker daemon dynamically
        with subprocess.Popen(["docker", "export", container_id], stdout=subprocess.PIPE) as proc:
            with tarfile.open(fileobj=proc.stdout, mode="r|") as tar:
                for member in tar:
                    if member.isfile():
                        name = member.name
                        
                        # Look for compiled artifacts
                        if name.endswith('.jar') or name.endswith('.war') or name.endswith('.ear'):
                            
                            # Normalize path with a leading slash for accurate and consistent exclusion matching
                            normalized_name = "/" + name.lstrip("/")
                            
                            # Ensure it's not an internal system, dependency library, or base server framework file
                            if not any(ign in normalized_name for ign in ignore_substrings):
                                filename = os.path.basename(name)
                                # Flatten the directory structure locally to isolate the artifact
                                dest_path = os.path.join(bin_dir, filename)
                                os.makedirs(os.path.dirname(dest_path), exist_ok=True)
                                
                                f_in = tar.extractfile(member)
                                if f_in:
                                    with open(dest_path, "wb") as f_out:
                                        shutil.copyfileobj(f_in, f_out)
                                    extracted = True
                                    if filename not in main_artifacts:
                                        main_artifacts.append(filename)
                                    logging.info(f"  -> Discovered Main Artifact: {name}")
    except Exception as e:
        logging.error(f"Dynamic filesystem scan failed: {e}")

    # Fallback to copy /app.jar directly if tar logic failed for some edge condition
    if not extracted:
        cp_proc = subprocess.run(["docker", "cp", f"{container_id}:/app.jar", bin_dir], capture_output=True)
        if cp_proc.returncode == 0 and os.path.exists(os.path.join(bin_dir, "app.jar")):
             logging.info("Extracted /app.jar directly from root.")
             extracted = True
             main_artifacts.append("app.jar")

    if not main_artifacts:
        logging.error(f"Failed to discover ANY main .jar or .war applications inside the container {current_image}. Aborting.")
        step_cleanup(app_name, current_image, container_id, extract_dir, cleanup)
        return False

    if len(main_artifacts) > 1:
        logging.warning(f"Multiple artifacts found. Analysis will be triggered primarily on: {main_artifacts[0]}")

    # 4. MTA App Creation
    logging.info("4. Synchronizing MTA Application definitions...")
    headers = {
        "Content-Type": "application/json"
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
        
    # Idempotency: Auto-purge the old application on re-runs to avoid 409 Conflicts or duplicating data
    try:
        existing_resp = requests.get(f"{url}/hub/applications", headers=headers, verify=False)
        if existing_resp.status_code == 200:
            for app in existing_resp.json():
                if app.get("name") == app_name:
                    old_id = app.get("id")
                    logging.info(f"Existing MTA application detected matching '{app_name}'. Purging old record (ID: {old_id}) to cleanly reset...")
                    requests.delete(f"{url}/hub/applications/{old_id}", headers=headers, verify=False)
                    time.sleep(2) # brief pause to let db reconcile
    except Exception as e:
        logging.warning(f"Pre-flight application cleanup failed for {app_name}. Continuing... Error: {e}")
        
    logging.info("Creating fresh MTA Application...")
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
        step_cleanup(app_name, current_image, container_id, extract_dir, cleanup)
        return False

    # We'll defer uploading the binaries until we create the TaskGroup,
    # as the MTA UI natively uploads local binaries to the TaskGroup bucket instead of the Application bucket.


    # 5. Assessment taskgroup configuration
    # Note: binary=True forces MTA to evaluate the JARs natively
    logging.info("5. Running Assessment...")
    targets_list = [t.strip() for t in targets_csv.split(",")]
    
    assessment_data = {
        "name": f"assessment-{app_name}",
        "addon": "analyzer",
        "state": "Created", # We create it first without submitting so we can upload and update it
        "data": {
            "mode": {
                "artifact": "", # We will update this after generating the taskgroup ID and uploading
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
        # Create TaskGroup
        tg_resp = requests.post(f"{url}/hub/taskgroups", headers=headers, json=assessment_data, verify=False)
        tg_resp.raise_for_status()
        tg_data = tg_resp.json()
        tg_id = tg_data.get("id")
        logging.info(f"Assessment TaskGroup created successfully: ID={tg_id}")

        # Upload raw binaries natively into the taskgroup bucket mirroring the UI behavior
        logging.info(f"Uploading {len(main_artifacts)} raw application binaries to taskgroup bucket...")
        upload_headers = {}
        if token:
            upload_headers["Authorization"] = f"Bearer {token}"

        uploaded_files_count = 0
        for artifact in main_artifacts:
            artifact_path = os.path.join(bin_dir, artifact)
            with open(artifact_path, "rb") as f:
                logging.info(f"  -> Uploading pure binary to taskgroup: {artifact}")
                
                # Removing 'binary/' path segment as Konveyor's backend os.Create likely panics (HTTP 500) if the folder doesn't exist
                upload_resp = requests.post(
                    f"{url}/hub/taskgroups/{tg_id}/bucket",
                    headers=upload_headers,
                    files={"file": (artifact, f, "application/java-archive")},
                    verify=False
                )
                if upload_resp.status_code in [200, 201, 204]:
                    uploaded_files_count += 1
                else:
                    logging.error(f"TaskGroup file upload failed for {artifact} with HTTP code: {upload_resp.status_code}. Server Response: {upload_resp.text}")

        if uploaded_files_count == 0:
            logging.error("Failed to upload ANY application binaries to the taskgroup bucket. Aborting.")
            step_cleanup(app_name, current_image, container_id, extract_dir, cleanup)
            return False
            
        logging.info("All main application binaries uploaded to TaskGroup bucket successfully.")

        # Update TaskGroup with the correct fully qualified hub artifact path
        # Using the fully populated tg_data object from the server to prevent DB null-field panics
        tg_data["data"]["mode"]["artifact"] = f"/hub/taskgroups/{tg_id}/bucket/{main_artifacts[0]}"
        
        update_resp = requests.put(f"{url}/hub/taskgroups/{tg_id}", headers=headers, json=tg_data, verify=False)
        update_resp.raise_for_status()

        # Submit TaskGroup to trigger analysis
        submit_resp = requests.post(f"{url}/hub/taskgroups/{tg_id}/submit", headers=headers, json={}, verify=False)
        submit_resp.raise_for_status()
        logging.info(f"Assessment triggered successfully for {app_name}!")
        
    except requests.exceptions.HTTPError as he:
        logging.error(f"MTA API HTTP Error during taskgroup operations: {he}")
        if he.response is not None:
            logging.error(f"Server diagnostic message: {he.response.text}")
        step_cleanup(app_name, current_image, container_id, extract_dir, cleanup)
        return False
    except Exception as e:
        logging.exception(f"Unexpected local failure during assessment initiation: {e}")
        step_cleanup(app_name, current_image, container_id, extract_dir, cleanup)
        return False

    step_cleanup(app_name, current_image, container_id, extract_dir, cleanup)
    return True

def step_cleanup(app_name, current_image, container_id, extract_dir, cleanup):
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

def main():
    parser = argparse.ArgumentParser(description="MTA Utility v3 Python - Windows/Cross-Platform")
    parser.add_argument("-c", "--csv", required=True, help="CSV file containing: app_name,image_url,namespace")
    parser.add_argument("-u", "--url", required=True, help="MTA Hub URL (e.g., https://mta...apps.cluster.com)")
    parser.add_argument("-t", "--token", required=False, default=None, help="Bearer token for OpenShift/MTA authentication (optional if Auth is disabled)")
    parser.add_argument("-g", "--targets", default="cloud-readiness", help="Comma-separated list of assessment targets")
    parser.add_argument("-k", "--keep", action="store_true", help="Keep the extracted local artifacts")
    parser.add_argument("--skip-oc", action="store_true", help="Skip fetching Kubernetes/OpenShift manifests")
    
    args = parser.parse_args()

    setup_logging()

    if not os.path.exists(args.csv):
        logging.error(f"CSV file '{args.csv}' does not exist.")
        sys.exit(1)

    url_clean = args.url.rstrip("/")

    check_prereqs(args.skip_oc)
    check_mta_api(url_clean, args.token)

    start_time = time.time()
    logging.info(f"Starting CSV batch processing from {args.csv}...")

    tasks = []
    with open(args.csv, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        for row in reader:
            if not row or "".join(row).strip() == "" or row[0].startswith("#"):
                continue
            
            if len(row) >= 2:
                app_name = row[0].strip()
                img_url = row[1].strip()
                ns = row[2].strip() if len(row) >= 3 else ""
                
                if not app_name or not img_url or "app_name" in app_name:
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
                logging.exception(f"Critical exception processing {app_name}: {e}")
                failure_count += 1

    duration = int(time.time() - start_time)
    logging.info("===========================================")
    logging.info(f"All tasks complete. Total execution time: {duration} seconds.")
    logging.info(f"Summary: {success_count} Succeeded, {failure_count} Failed.")
    logging.info("===========================================")

if __name__ == "__main__":
    main()
