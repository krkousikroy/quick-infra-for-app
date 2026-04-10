# MTA Workflow Automation (Python)

This Python script is the next-generation, cross-platform replacement for the MTA (Migration Toolkit for Applications) bash-based automation scripts. It natively handles OpenShift application assessment workflow through dynamic container scanning without relying on `zip` or manual paths.

## Key Upgrades over Bash versions:
- **Parallel Processing Engine**: Processes multiple Docker images at the same time to drastically cut execution time.
- **Flawless Artifact Sniffing**: Safely ignores system Java classes and dynamically streams the actual container file system to grab true `.jar`, `.war`, and `.ear` application binaries dynamically, without guessing directory paths.
- **Idempotent Automation**: Gracefully clears out old/duplicate MTA Applications before retrying to prevent `409 Conflicts`.
- **Offline / Local Container Support**: Automatically queries your cached `docker daemon` and supports disconnected operation.

---

## Prerequisites

1. **Python 3.x**
2. **Docker** (Must be running in the background)
3. **OpenShift CLI (`oc`)** (Optional, only required if you want to pull active cluster YAML manifests).

### Dependencies
Install the required `requests` library to communicate with the MTA REST API:
```bash
pip install -r requirements.txt
# (or just: pip install requests)
```

---

## The Input File (`input.csv`)
You must provide a comma-separated `.csv` file detailing which applications to process. **Do not include headers**. It strictly expects 3 columns per row:
```csv
# app_name, container_image_url, namespace
billing-app, quay.io/myorg/billing-service:v1, finance-prod
inventory-app, local-registry/supply/inventory:v4, warehouse-prod
```
> *Note: If you are skipping OpenShift connectivity entirely, you may leave the 3rd column (namespace) blank!*

---

## How to Run the Tool

The core execution takes your CSV file and points it directly at your MTA Hub API.

### 1. The Standard Execution
If your environment is protected by an OpenShift authentication token:
```cmd
python mta_workflow_win.py --csv input.csv --url https://mta-...apps.cluster.com --token YOUR_BEARER_TOKEN
```

### 2. The Authentication-Free Execution
If you are running a local unsecured MTA deployment, simply exclude the `--token` flag. The script dynamically adapts to an unauthenticated REST posture:
```cmd
python mta_workflow_win.py --csv input.csv --url http://YOUR_LOCAL_MTA_HUB
```

### 3. The Offline / Unconnected Execution (`--skip-oc`)
If you aren't connected to an OpenShift cluster or you just want to blind-test container images straight from a local Docker cache:
```cmd
python mta_workflow_win.py --csv input.csv --url http://YOUR_MTA_HUB --skip-oc
```

---

## Available Arguments
| Flag | Name | Required | Description |
|---|---|---|---|
| `-c`, `--csv` | CSV File | **YES** | Path to the `input.csv` workload file. |
| `-u`, `--url` | MTA URL | **YES** | The root URL for your MTA Hub. |
| `-t`, `--token` | Token | NO | Bearer token for accessing secure MTA instances. |
| `-g`, `--targets` | Assessment | NO | Comma-separated list of target architectures. *(Default: `cloud-readiness`)* |
| `--skip-oc` | Blind run | NO | Tells the script to completely ignore the `oc` command line tools and Kubernetes cluster manifest fetching. |
| `-k`, `--keep` | Debug Keep | NO | Leaves the raw `.zip` and `.jar` artifacts sitting on your hard drive after the script completes instead of aggressively cleaning them up. |

---

## Example Complex Execution
*(Analyzing the apps for Quarkus and Cloud-Readiness while preserving the artifacts locally and ignoring OpenShift connection)*:
```cmd
python mta_workflow_win.py --csv input.csv --url http://mta-hub --targets "cloud-readiness,quarkus" --skip-oc --keep
```
