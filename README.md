# MTA Workflow Automation Script

A cross-platform Python script for automating Migration Toolkit for Applications (MTA) workflows, including OpenShift manifest fetching, Docker image binary extraction, and MTA analysis submission.

## Features

- **Cross-Platform**: Works on Windows, Linux, and macOS
- **Dynamic Binary Extraction**: Automatically finds and extracts all `.jar`, `.war`, and `.ear` files from Docker containers
- **Optional OpenShift Integration**: Can skip OpenShift manifest fetching if not needed
- **MTA API Integration**: Creates applications, uploads artifacts, and triggers assessments
- **Robust Error Handling**: Includes retry logic, validation, and detailed logging
- **Flexible Authentication**: Supports both authenticated and non-authenticated MTA setups

## Prerequisites

- **Python 3.6+**
- **Docker Desktop** (running)
- **OpenShift CLI (`oc`)** (optional, if fetching manifests)
- **CSV file** with application data

### Installing Dependencies

The script uses only standard Python libraries (`requests`, `subprocess`, etc.), so no additional pip installs are required.

## Usage

### Basic Command Structure

```bash
python mta_workflow_win.py [OPTIONS]
```

### Required Arguments

- `-c, --csv`: Path to CSV file containing application data
- `-u, --url`: MTA Hub URL (e.g., `https://mta-hub.example.com`)

### Optional Arguments

- `-t, --token`: Bearer token for MTA authentication (optional if authentication is disabled)
- `-g, --targets`: Comma-separated list of assessment targets (default: `cloud-readiness`)
- `-k, --keep`: Keep extracted local artifacts after processing
- `--skip-oc`: Skip fetching OpenShift manifests
- `-v, --verbose`: Enable verbose (debug) logging
- `--dry-run`: Simulate operations without executing them

### CSV File Format

Create a CSV file with the following columns (no header row needed):

```csv
app_name,image_url,namespace
my-spring-app,registry.example.com/my-app:latest,my-namespace
another-app,registry.example.com/another-app:v1.0,another-ns
```

Example CSV content:
```
spring-boot-app,quay.io/example/spring-app:latest,spring-ns
jboss-app,registry.example.com/jboss-app:v2.1,jboss-ns
```

## Examples

### Full Workflow (with OpenShift manifests)

```bash
python mta_workflow_win.py \
  -c applications.csv \
  -u https://mta-hub.apps.cluster.example.com \
  -t eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9... \
  -g "cloud-readiness,openjdk17" \
  -v
```

### Skip OpenShift Manifests (Binaries Only)

```bash
python mta_workflow_win.py \
  -c applications.csv \
  -u https://mta-hub.example.com \
  --skip-oc \
  -k
```

### Non-Authenticated MTA Setup

```bash
python mta_workflow_win.py \
  -c applications.csv \
  -u https://mta-hub.example.com \
  --skip-oc
```

### Dry Run for Testing

```bash
python mta_workflow_win.py \
  -c applications.csv \
  -u https://mta-hub.example.com \
  --dry-run \
  -v
```

## Workflow Steps

1. **Validation**: Checks prerequisites (Docker, OpenShift CLI if needed)
2. **CSV Processing**: Reads application data from CSV
3. **Manifest Fetching** (optional): Downloads OpenShift resources (deployments, services, etc.)
4. **Binary Extraction**: Pulls Docker images and extracts Java binaries (.jar, .war, .ear)
5. **Archiving**: Creates ZIP with manifests and binaries
6. **MTA Integration**: Creates application, uploads ZIP, triggers analysis

## Output and Logging

- **Log File**: `mta_workflow_win_runtime.log` (appends to existing file)
- **Console Output**: Progress and error messages
- **Verbose Mode**: Detailed debug information with `-v`

### Log Levels

- **INFO**: General progress and results
- **WARNING**: Non-critical issues (e.g., missing files)
- **ERROR**: Failures that stop processing
- **DEBUG**: Detailed technical information (with `-v`)

## Troubleshooting

### Common Issues

1. **400 Bad Request on Upload**
   - Check ZIP file size and contents
   - Verify MTA version compatibility
   - Review response body in logs for specific error

2. **Docker Connection Failed**
   - Ensure Docker Desktop is running
   - Check Docker daemon permissions

3. **OpenShift Authentication Failed**
   - Run `oc login` if using `--skip-oc=false`
   - Verify cluster access

4. **No Binaries Found**
   - Check if container has Java applications
   - Use verbose logging to see extraction attempts

### Debug Mode

Enable verbose logging for detailed troubleshooting:

```bash
python mta_workflow_win.py -c apps.csv -u https://mta-url -v
```

### Checking Extracted Files

Use `--keep` to preserve temporary files for inspection:

```bash
python mta_workflow_win.py -c apps.csv -u https://mta-url --keep
```

Files will be in `mta-assessment-{app_name}/` directories.

## Security Notes

- Bearer tokens are sent in HTTP headers (use HTTPS in production)
- Docker images are pulled and containers created temporarily
- No sensitive data is stored permanently

## Performance Considerations

- **Concurrent Processing**: Processes up to 3 applications simultaneously
- **Binary Extraction**: May take time for large containers
- **Retry Logic**: Automatic retries for network issues
- **Cleanup**: Temporary files removed automatically (unless `--keep` used)

## Compatibility

- **MTA Versions**: Tested with 8.0.1+ (API endpoints verified)
- **Java Applications**: Supports Spring Boot, JBoss/Wildfly, WebLogic, and generic Java EE
- **Container Images**: Works with any Linux-based container with `find` command

## Support

For issues specific to MTA or Red Hat products, consult:
- [MTA Documentation](https://docs.redhat.com/en/documentation/migration_toolkit_for_applications/)
- Red Hat Support Portal

## License

This script is provided as-is for automation purposes. Ensure compliance with your organization's policies when using in production environments.</content>
<parameter name="filePath">mta-analyze/README.md