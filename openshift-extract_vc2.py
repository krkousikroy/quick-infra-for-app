import argparse
import csv
import json
import logging
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, Iterable, List, Set, Tuple

ResourceCounts = Dict[str, Dict[str, int]]
SYSTEM_NAMESPACES: Set[str] = {
    "default",
    "openshift",
    "kube-system",
    "kube-public",
    "kube-node-lease",
}
SYSTEM_NAMESPACE_PREFIXES = ("openshift-", "kube-")

# Mapping of display row name to OpenShift/Kubernetes resource type
NAMESPACED_RESOURCES: Dict[str, str] = {
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
    "EnvVars (in Workloads)": "SPECIAL_ENV_VARS",
}

SPECIAL_ENV_VARS = "SPECIAL_ENV_VARS"


def setup_logging(debug_mode: bool) -> None:
    """Configure logging for console and file output."""
    logger = logging.getLogger()
    for handler in list(logger.handlers):
        logger.removeHandler(handler)

    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    level = logging.DEBUG if debug_mode else logging.INFO

    file_handler = logging.FileHandler("openshift-extract.log", mode="w", encoding="utf-8")
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)

    logger.setLevel(level)
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)


def positive_int(value: str) -> int:
    """Validate that an argument is a positive integer."""
    try:
        ivalue = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid int value: '{value}'")
    if ivalue <= 0:
        raise argparse.ArgumentTypeError(f"value must be greater than 0: '{value}'")
    return ivalue


def parse_namespace_list(raw_value: str) -> Set[str]:
    """Parse a comma-separated namespace list into a normalized set."""
    return {ns.strip() for ns in raw_value.split(",") if ns.strip()} if raw_value else set()


def oc_installed() -> bool:
    """Return True if the oc CLI is available on PATH."""
    return shutil.which("oc") is not None


def run_cmd(cmd_list: List[str]) -> str:
    """Execute an oc command and return stdout, returning empty string on expected failures."""
    try:
        logging.debug("Executing command: %s", " ".join(cmd_list))
        startupinfo = None
        if sys.platform == "win32":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

        result = subprocess.run(
            cmd_list,
            capture_output=True,
            text=True,
            check=True,
            startupinfo=startupinfo,
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip().lower()
        logging.debug("Command failed (%s): %s", " ".join(cmd_list), stderr)
        if any(token in stderr for token in (
            "the server doesn't have a resource type",
            "no resources found",
            "not found",
        )):
            return ""
        return ""
    except FileNotFoundError:
        logging.error("The 'oc' command was not found. Ensure the OpenShift CLI is installed and in your PATH.")
        sys.exit(1)


def is_system_namespace(namespace: str) -> bool:
    """Return True if the namespace is a standard OpenShift/Kubernetes system namespace."""
    return namespace in SYSTEM_NAMESPACES or namespace.startswith(SYSTEM_NAMESPACE_PREFIXES)


def get_namespaces() -> List[str]:
    """Retrieve all namespaces the user has access to."""
    logging.info("Fetching namespaces...")
    output = run_cmd([
        "oc",
        "get",
        "namespaces",
        "-o",
        "custom-columns=NAME:.metadata.name",
        "--no-headers",
    ])
    if not output:
        logging.error("Failed to fetch namespaces or no namespaces found.")
        return []
    return sorted(line.strip() for line in output.splitlines() if line.strip())


def get_special_env_vars_count(namespace: str, resource_name: str) -> Tuple[str, str, int]:
    """Count env and envFrom declarations inside workload objects in a namespace."""
    cmd = [
        "oc",
        "get",
        "deployments,deploymentconfigs,statefulsets,daemonsets,cronjobs",
        "-n",
        namespace,
        "-o",
        "json",
        "--ignore-not-found",
    ]
    output = run_cmd(cmd)
    if not output:
        return namespace, resource_name, 0

    try:
        data = json.loads(output)
    except json.JSONDecodeError:
        logging.warning("Unable to decode JSON when counting env vars in namespace %s.", namespace)
        return namespace, resource_name, 0

    def count_env(obj: object) -> int:
        total = 0
        if isinstance(obj, dict):
            for key, value in obj.items():
                if key == "env" and isinstance(value, list):
                    total += len(value)
                elif key == "envFrom" and isinstance(value, list):
                    total += len(value)
                else:
                    total += count_env(value)
        elif isinstance(obj, list):
            for item in obj:
                total += count_env(item)
        return total

    return namespace, resource_name, count_env(data)


def get_current_deployment_count(namespace: str, resource_name: str) -> Tuple[str, str, int]:
    """Count only the currently meaningful deployments in a namespace."""
    cmd = [
        "oc",
        "get",
        "deployments",
        "-n",
        namespace,
        "-o",
        "json",
        "--ignore-not-found",
    ]
    output = run_cmd(cmd)
    if not output:
        return namespace, resource_name, 0

    try:
        data = json.loads(output)
    except json.JSONDecodeError:
        logging.warning("Unable to decode JSON when counting deployments in namespace %s.", namespace)
        return namespace, resource_name, 0

    def is_current(deployment: dict) -> bool:
        status = deployment.get("status", {})
        conditions = status.get("conditions", [])
        if any(
            cond.get("type") == "Available" and cond.get("status") == "True"
            for cond in conditions
            if isinstance(cond, dict)
        ):
            return True
        if status.get("readyReplicas", 0) > 0:
            return True
        if status.get("availableReplicas", 0) > 0:
            return True
        return False

    items = data.get("items", []) if isinstance(data, dict) else []
    current_count = sum(1 for deployment in items if is_current(deployment))
    return namespace, resource_name, current_count


def _parse_revision(annotation_value: str) -> int:
    try:
        return int(annotation_value)
    except (TypeError, ValueError):
        return 0


def _creation_timestamp(value: str) -> str:
    return value or ""


def get_current_replica_set_count(namespace: str, resource_name: str) -> Tuple[str, str, int]:
    """Count only the current replica set per deployment in a namespace."""
    cmd = [
        "oc",
        "get",
        "replicasets",
        "-n",
        namespace,
        "-o",
        "json",
        "--ignore-not-found",
    ]
    output = run_cmd(cmd)
    if not output:
        return namespace, resource_name, 0

    try:
        data = json.loads(output)
    except json.JSONDecodeError:
        logging.warning("Unable to decode JSON when counting replicasets in namespace %s.", namespace)
        return namespace, resource_name, 0

    latest_by_deployment = {}
    orphan_active_count = 0

    for rs in data.get("items", []):
        metadata = rs.get("metadata", {})
        annotations = metadata.get("annotations") or {}
        revision = _parse_revision(annotations.get("deployment.kubernetes.io/revision"))
        creation = _creation_timestamp(metadata.get("creationTimestamp"))

        owner_refs = metadata.get("ownerReferences", []) or []
        deployment_owner = next(
            (owner.get("name") for owner in owner_refs if owner.get("kind") == "Deployment"),
            None,
        )

        if deployment_owner:
            current = latest_by_deployment.get(deployment_owner)
            candidate = (revision, creation, rs)
            if current is None or (revision, creation) > (current[0], current[1]):
                latest_by_deployment[deployment_owner] = candidate
            continue

        status = rs.get("status", {})
        if status.get("replicas", 0) > 0 or status.get("readyReplicas", 0) > 0:
            orphan_active_count += 1

    return namespace, resource_name, len(latest_by_deployment) + orphan_active_count


def get_current_job_count(namespace: str, resource_name: str) -> Tuple[str, str, int]:
    """Count only meaningful jobs, collapsing CronJob history into one entry."""
    cmd = [
        "oc",
        "get",
        "jobs",
        "-n",
        namespace,
        "-o",
        "json",
        "--ignore-not-found",
    ]
    output = run_cmd(cmd)
    if not output:
        return namespace, resource_name, 0

    try:
        data = json.loads(output)
    except json.JSONDecodeError:
        logging.warning("Unable to decode JSON when counting jobs in namespace %s.", namespace)
        return namespace, resource_name, 0

    current_jobs = 0
    cronjob_latest = {}

    def job_key(job: dict) -> str:
        metadata = job.get("metadata", {})
        return metadata.get("creationTimestamp", "")

    for job in data.get("items", []):
        metadata = job.get("metadata", {})
        owner_refs = metadata.get("ownerReferences", []) or []
        cronjob_owner = next(
            (owner.get("name") for owner in owner_refs if owner.get("kind") == "CronJob"),
            None,
        )

        if cronjob_owner:
            existing = cronjob_latest.get(cronjob_owner)
            if existing is None or job_key(job) > job_key(existing):
                cronjob_latest[cronjob_owner] = job
            continue

        status = job.get("status", {})
        if status.get("active", 0) > 0 or not status.get("completionTime"):
            current_jobs += 1

    current_jobs += len(cronjob_latest)
    return namespace, resource_name, current_jobs


def get_current_build_count(namespace: str, resource_name: str) -> Tuple[str, str, int]:
    """Count only the latest build per BuildConfig entry."""
    cmd = [
        "oc",
        "get",
        "builds",
        "-n",
        namespace,
        "-o",
        "json",
        "--ignore-not-found",
    ]
    output = run_cmd(cmd)
    if not output:
        return namespace, resource_name, 0

    try:
        data = json.loads(output)
    except json.JSONDecodeError:
        logging.warning("Unable to decode JSON when counting builds in namespace %s.", namespace)
        return namespace, resource_name, 0

    latest_by_buildconfig = {}
    orphan_active_count = 0

    for build in data.get("items", []):
        metadata = build.get("metadata", {})
        annotations = metadata.get("annotations") or {}
        build_number = _parse_revision(annotations.get("openshift.io/build.number"))
        creation = _creation_timestamp(metadata.get("creationTimestamp"))

        owner_refs = metadata.get("ownerReferences", []) or []
        buildconfig_owner = next(
            (owner.get("name") for owner in owner_refs if owner.get("kind") == "BuildConfig"),
            None,
        )

        if buildconfig_owner:
            current = latest_by_buildconfig.get(buildconfig_owner)
            candidate = (build_number, creation, build)
            if current is None or (build_number, creation) > (current[0], current[1]):
                latest_by_buildconfig[buildconfig_owner] = candidate
            continue

        status = build.get("status", {})
        if status.get("phase") in {"New", "Pending", "Running"}:
            orphan_active_count += 1

    return namespace, resource_name, len(latest_by_buildconfig) + orphan_active_count


def get_resource_count(namespace: str, resource_name: str, resource_type: str) -> Tuple[str, str, int]:
    """Get the count of a specific resource within a namespace."""
    if resource_type == SPECIAL_ENV_VARS:
        return get_special_env_vars_count(namespace, resource_name)

    if resource_type == "deployments":
        return get_current_deployment_count(namespace, resource_name)
    if resource_type == "replicasets":
        return get_current_replica_set_count(namespace, resource_name)
    if resource_type == "jobs":
        return get_current_job_count(namespace, resource_name)
    if resource_type == "builds":
        return get_current_build_count(namespace, resource_name)

    cmd = [
        "oc",
        "get",
        resource_type,
        "-n",
        namespace,
        "-o",
        "custom-columns=NAME:.metadata.name",
        "--no-headers",
        "--ignore-not-found",
    ]
    output = run_cmd(cmd)
    if not output:
        return namespace, resource_name, 0

    return namespace, resource_name, sum(1 for line in output.splitlines() if line.strip())


def check_authentication() -> None:
    """Verify that the user is currently logged into an OpenShift cluster."""
    logging.info("Verifying OpenShift session...")
    output = run_cmd(["oc", "whoami"])
    if not output:
        logging.error("Authentication failed or session expired. Please log in to your OpenShift cluster using 'oc login' first.")
        sys.exit(1)
    logging.info("Authenticated successfully as: %s", output)


def write_csv_report(
    output_filename: str,
    namespaces: Iterable[str],
    results: ResourceCounts,
    namespace_times: Dict[str, float],
) -> None:
    """Write namespace resource counts to a CSV report."""
    output_path = Path(output_filename).expanduser()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    headers = ["Namespace"] + list(NAMESPACED_RESOURCES.keys())
    with output_path.open("w", newline="", encoding="utf-8") as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=headers)
        writer.writeheader()

        for namespace in namespaces:
            row = {"Namespace": namespace}
            row.update(results[namespace])
            writer.writerow(row)


def collect_namespace_resource_counts(namespaces: List[str], maximum_workers: int) -> Tuple[ResourceCounts, Dict[str, float]]:
    """Collect namespaced resource counts in parallel."""
    results = {namespace: {name: 0 for name in NAMESPACED_RESOURCES} for namespace in namespaces}
    namespace_stats = {
        namespace: {"completed": 0, "elapsed": 0.0}
        for namespace in namespaces
    }
    total_tasks = len(namespaces) * len(NAMESPACED_RESOURCES)
    if total_tasks == 0:
        return results, {namespace: 0.0 for namespace in namespaces}

    max_workers = max(1, min(maximum_workers, total_tasks))
    logging.info("Fetching namespace resources (Total queries: %s, workers: %s).", total_tasks, max_workers)

    completed_tasks = 0
    progress_step = max(1, total_tasks // 20)
    start_time = time.monotonic()
    namespace_resource_count = len(NAMESPACED_RESOURCES)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_query = {
            executor.submit(get_resource_count, namespace, resource_name, resource_type): (
                namespace,
                resource_name,
            )
            for namespace in namespaces
            for resource_name, resource_type in NAMESPACED_RESOURCES.items()
        }

        for future in as_completed(future_to_query):
            namespace, resource_name = future_to_query[future]
            try:
                _, _, count = future.result()
            except Exception as exc:
                logging.warning(
                    "Failed to count %s in namespace %s: %s",
                    resource_name,
                    namespace,
                    exc,
                )
                count = 0
            results[namespace][resource_name] = count
            namespace_stats[namespace]["completed"] += 1
            if namespace_stats[namespace]["completed"] == namespace_resource_count:
                namespace_stats[namespace]["elapsed"] = time.monotonic() - start_time

            completed_tasks += 1
            if completed_tasks % progress_step == 0 or completed_tasks == total_tasks:
                logging.info("Progress: %s/%s queries completed.", completed_tasks, total_tasks)

    return results, {ns: stats["elapsed"] for ns, stats in namespace_stats.items()}


def print_summary(namespaces: List[str], results: ResourceCounts, namespace_times: Dict[str, float]) -> None:
    """Print a summary of the collected data."""
    logging.info("Collected resource counts for %s namespaces.", len(namespaces))
    for namespace in namespaces:
        logging.info("Namespace %s completed in %.2f seconds.", namespace, namespace_times.get(namespace, 0.0))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Extract OpenShift resource counts per namespace.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--limit", type=int, default=0, help="Limit the number of namespaces to process (0 for all)")
    parser.add_argument(
        "--exclude",
        type=str,
        default="",
        help="Comma-separated list of exact namespace names to exclude",
    )
    parser.add_argument(
        "--include-system",
        action="store_true",
        help="Include standard OpenShift/Kubernetes system namespaces",
    )
    parser.add_argument(
        "--output",
        type=str,
        default="openshift_resource_counts.csv",
        help="Custom output CSV file name",
    )
    parser.add_argument(
        "--workers",
        type=positive_int,
        default=20,
        help="Number of parallel processing workers",
    )
    parser.add_argument("--debug", action="store_true", help="Enable verbose debug logging")
    args = parser.parse_args()

    setup_logging(args.debug)
    logging.info("Starting OpenShift Extractor utility...")

    if not oc_installed():
        logging.error("The 'oc' CLI is required but was not found in PATH.")
        sys.exit(1)

    check_authentication()

    all_namespaces = get_namespaces()
    if not all_namespaces:
        return

    excluded_namespaces = parse_namespace_list(args.exclude)
    if not args.include_system:
        namespaces = [
            ns
            for ns in all_namespaces
            if not is_system_namespace(ns) and ns not in excluded_namespaces
        ]
        logging.info(
            "Discovered %s namespaces total. Filtered out %s system/excluded namespaces. Remaining: %s.",
            len(all_namespaces),
            len(all_namespaces) - len(namespaces),
            len(namespaces),
        )
    else:
        namespaces = [ns for ns in all_namespaces if ns not in excluded_namespaces]
        logging.info(
            "Discovered %s namespaces total. Including system namespaces. Excluded: %s.",
            len(all_namespaces),
            len(excluded_namespaces),
        )

    if args.limit > 0:
        namespaces = namespaces[: args.limit]
        logging.info("Applied limit flag: Only processing the first %s namespaces.", len(namespaces))

    if not namespaces:
        logging.warning("No namespaces left to process after filtering.")
        return

    results, namespace_times = collect_namespace_resource_counts(namespaces, args.workers)
    print_summary(namespaces, results, namespace_times)

    logging.info("Writing results to %s...", args.output)
    try:
        write_csv_report(args.output, namespaces, results, namespace_times)
        logging.info("Data successfully exported to %s", args.output)
    except OSError as exc:
        logging.error("Failed to write to file %s: %s", args.output, exc)


if __name__ == "__main__":
    main()
