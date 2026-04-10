import subprocess
import csv
import sys
import logging
import json
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed

def setup_logging(debug_mode):
    """Dynamically set up logging to console and a log file."""
    level = logging.DEBUG if debug_mode else logging.INFO
    
    # Remove all existing handlers to ensure a clean slate
    root = logging.getLogger()
    if root.handlers:
        for handler in root.handlers:
            root.removeHandler(handler)
            
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    
    # 1. File handler to persist logs into a file directly 
    fh = logging.FileHandler('openshift-extract.log', mode='w')
    fh.setFormatter(formatter)
    
    # 2. Console handler to output live progress cleanly
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(formatter)
    
    root.setLevel(level)
    root.addHandler(fh)
    root.addHandler(ch)

# Mapping of display row name to OpenShift/Kubernetes resource type
NAMESPACED_RESOURCES = {
    "Pods": "pods",
    "Deployments": "deployments",
    "Services": "services",
    "Routes": "routes",
    "Ingresses": "ingresses",
    "Builds": "builds",
    "BuildConfigs": "buildconfigs",
    "DeploymentConfigs": "deploymentconfigs",
    "PVCs": "persistentvolumeclaims", 
    "ImageStreams": "imagestreams",
    "HPAs": "horizontalpodautoscalers",
    "Roles": "roles",
    "RoleBindings": "rolebindings",
    "NetworkPolicies": "networkpolicies",
    "ConfigMaps": "configmaps",
    "Secrets": "secrets",
    "StatefulSets": "statefulsets",
    "DaemonSets": "daemonsets",
    "Jobs": "jobs",
    "CronJobs": "cronjobs",
    "ReplicaSets": "replicasets",
    "EnvVars (in Workloads)": "SPECIAL_ENV_VARS"
}

# Resources that are NOT namespace-bound, but cluster-wide
CLUSTER_RESOURCES = {
    "PVs": "persistentvolumes"
}

def run_cmd(cmd_list):
    """Run an oc command and return its standard output securely."""
    try:
        logging.debug(f"Executing command: {' '.join(cmd_list)}")
        # Hide console window on Windows to prevent flash of cmd prompt per execution
        startupinfo = None
        if sys.platform == "win32":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

        result = subprocess.run(
            cmd_list,
            capture_output=True,
            text=True,
            check=True,
            startupinfo=startupinfo
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError as e:
        # If resource type is unsupported on the cluster, politely return blank
        if "the server doesn't have a resource type" in e.stderr:
            return ""
        return ""
    except FileNotFoundError:
        logging.error("The 'oc' command was not found. Ensure the OpenShift CLI is installed and in your PATH.")
        sys.exit(1)

def is_system_namespace(ns):
    """Check if the namespace is an OpenShift or Kubernetes system namespace."""
    if ns in ["default", "openshift"]:
        return True
    if ns.startswith("openshift-") or ns.startswith("kube-"):
        return True
    return False

def get_namespaces():
    """Retrieve all namespaces the user has access to."""
    logging.info("Fetching namespaces...")
    output = run_cmd(["oc", "get", "namespaces", "-o", "custom-columns=NAME:.metadata.name", "--no-headers"])
    if not output:
        logging.error("Failed to fetch namespaces or no namespaces found.")
        return []
    return sorted([line.strip() for line in output.split('\n') if line.strip()])

def get_special_env_vars_count(namespace, resource_name):
    """
    Counts the number of environment variables defined inside main workload constructs
    like Deployments, DeploymentConfigs, StatefulSets, DaemonSets, and CronJobs in a namespace.
    """
    cmd = ["oc", "get", "deployments,deploymentconfigs,statefulsets,daemonsets,cronjobs", "-n", namespace, "-o", "json", "--ignore-not-found"]
    output = run_cmd(cmd)
    if not output:
        return namespace, resource_name, 0
    try:
        data = json.loads(output)
    except json.JSONDecodeError:
        return namespace, resource_name, 0
        
    def count_env(obj):
        c = 0
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k == "env" and isinstance(v, list):
                    c += len(v)
                elif k == "envFrom" and isinstance(v, list):
                    c += len(v)  # counts each envFrom block as 1 referenced source
                else:
                    c += count_env(v)
        elif isinstance(obj, list):
            for item in obj:
                c += count_env(item)
        return c
        
    return namespace, resource_name, count_env(data)

def get_resource_count(namespace, resource_name, resource_type):
    """Get the count of a specific resource within a given namespace."""
    if resource_type == "SPECIAL_ENV_VARS":
        return get_special_env_vars_count(namespace, resource_name)

    # Use custom-columns to just extract the name (lightweight compared to JSON output)
    cmd = ["oc", "get", resource_type, "-n", namespace, "-o", "custom-columns=NAME:.metadata.name", "--no-headers", "--ignore-not-found"]
    output = run_cmd(cmd)
    
    if not output:
        return namespace, resource_name, 0
    
    count = len([line for line in output.split('\n') if line.strip()])
    return namespace, resource_name, count

def get_cluster_resource_count(resource_name, resource_type):
    """Get the count of a cluster-wide resource (like PV)."""
    cmd = ["oc", "get", resource_type, "-o", "custom-columns=NAME:.metadata.name", "--no-headers", "--ignore-not-found"]
    output = run_cmd(cmd)
    
    if not output:
        return resource_name, 0
    
    count = len([line for line in output.split('\n') if line.strip()])
    return resource_name, count

def check_authentication():
    """Verify that the user is currently logged into an OpenShift cluster."""
    logging.info("Verifying OpenShift session...")
    output = run_cmd(["oc", "whoami"])
    if not output:
        logging.error("Authentication failed or session expired. Please log in to your OpenShift cluster using 'oc login' first.")
        sys.exit(1)
    logging.info(f"Authenticated successfully as: {output}")

def main():
    parser = argparse.ArgumentParser(description="Extract OpenShift resource counts per namespace.")
    parser.add_argument("--limit", type=int, default=0, help="Limit the number of namespaces to process (0 for all)")
    parser.add_argument("--exclude", type=str, default="", help="Comma-separated list of exact namespace names to aggressively exclude")
    parser.add_argument("--include-system", action="store_true", help="Include default OpenShift/Kubernetes system namespaces (e.g. kube-*, openshift-*)")
    parser.add_argument("--output", type=str, default="openshift_resource_counts.csv", help="Custom output CSV file name")
    parser.add_argument("--workers", type=int, default=20, help="Number of parallel processing workers (default: 20)")
    parser.add_argument("--debug", action="store_true", help="Enable verbose debug logging")
    args = parser.parse_args()

    # Establish full reporting system based on flags
    setup_logging(args.debug)
    logging.info("Starting OpenShift Extractor utility...")

    check_authentication()

    all_namespaces = get_namespaces()
    if not all_namespaces:
        return

    excluded_list = [ns.strip() for ns in args.exclude.split(',')] if args.exclude else []

    if not args.include_system:
        # Filter out OpenShift related system namespaces & manually excluded items
        namespaces = [ns for ns in all_namespaces if not is_system_namespace(ns) and ns not in excluded_list]
        logging.info(f"Discovered {len(all_namespaces)} namespaces total. Filtered out {len(all_namespaces) - len(namespaces)} workspaces (system rules & exclusions). Remaining: {len(namespaces)} user namespaces.")
    else:
        # Only filter user-defined omissions
        namespaces = [ns for ns in all_namespaces if ns not in excluded_list]
        if args.exclude:
            logging.info(f"Discovered {len(all_namespaces)} namespaces total. Filtered out {len(all_namespaces) - len(namespaces)} manually excluded workspaces.")
        else:
            logging.info(f"Discovered {len(all_namespaces)} namespaces total. Including system namespaces as requested.")

    if args.limit > 0:
        namespaces = namespaces[:args.limit]
        logging.info(f"Applied limit flag: Only processing the first {len(namespaces)} namespaces.")

    if not namespaces:
        logging.warning("No namespaces left to process after filtering.")
        return

    # Initialize results mapping
    results = {ns: {res: 0 for res in NAMESPACED_RESOURCES} for ns in namespaces}

    # Gather cluster-wide resources (like PersistentVolumes)
    logging.info("Fetching cluster-wide resources (like PVs)...")
    cluster_results = {}
    for res_name, res_type in CLUSTER_RESOURCES.items():
        _, count = get_cluster_resource_count(res_name, res_type)
        cluster_results[res_name] = count

    # Using ThreadPoolExecutor to run OpenShift queries in parallel
    max_workers = args.workers 
    total_tasks = len(namespaces) * len(NAMESPACED_RESOURCES)
    completed_tasks = 0

    logging.info(f"Fetching namespace resources (Total individual queries: {total_tasks}). This might take a while...")

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = []
        for ns in namespaces:
            for res_name, res_type in NAMESPACED_RESOURCES.items():
                futures.append(executor.submit(get_resource_count, ns, res_name, res_type))
        
        for future in as_completed(futures):
            ns, res_name, count = future.result()
            results[ns][res_name] = count
            
            completed_tasks += 1
            if completed_tasks % 100 == 0 or completed_tasks == total_tasks:
                logging.info(f"Progress: {completed_tasks}/{total_tasks} queries completed...")

    # Output a clean summary report to the console before concluding
    logging.info("\n--- SUMMARY REPORT ---")
    for res_name in NAMESPACED_RESOURCES.keys():
        total = sum(results[ns][res_name] for ns in namespaces)
        logging.info(f"Total {res_name} across processed namespaces: {total}")
    for res_name, count in cluster_results.items():
        logging.info(f"Total {res_name} (Cluster-wide): {count}")
    logging.info("----------------------\n")

    output_filename = args.output
    
    headers = ["Namespace"] + list(NAMESPACED_RESOURCES.keys()) + list(CLUSTER_RESOURCES.keys())
    
    logging.info(f"Writing results to {output_filename}...")
    try:
        with open(output_filename, 'w', newline='', encoding='utf-8') as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=headers)
            writer.writeheader()
            
            # Write each namespace as a row
            for ns in namespaces:
                row = {"Namespace": ns}
                row.update(results[ns])
                for cr in CLUSTER_RESOURCES.keys():
                    row[cr] = "N/A"
                writer.writerow(row)
                
            # Write dynamic aggregate TOTAL row for all mathematically parsed namespaces
            total_row = {"Namespace": "=== TOTAL ==="}
            for res_name in NAMESPACED_RESOURCES.keys():
                total_row[res_name] = sum(results[ns][res_name] for ns in namespaces)
            for cr in CLUSTER_RESOURCES.keys():
                total_row[cr] = "N/A"
            writer.writerow(total_row)

            # Write cluster-wide resources as the final row appending beneath totals
            cluster_row = {"Namespace": "=== CLUSTER WIDE ==="}
            for nr in NAMESPACED_RESOURCES.keys():
                cluster_row[nr] = "N/A"
            cluster_row.update(cluster_results)
            writer.writerow(cluster_row)
                
        logging.info(f"Data successfully exported to {output_filename}")
        
    except IOError as e:
        logging.error(f"Failed to write to file {output_filename}: {e}")

if __name__ == "__main__":
    main()
