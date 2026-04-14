#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MTA (Migration Toolkit for Applications) Analyzer
Automates the process of analyzing applications using Red Hat MTA 8.0.1
"""

import csv
import json
import logging
import os
import subprocess
import sys
import tempfile
import shutil
import io
import tarfile
from pathlib import Path
from typing import List, Dict, Optional, Tuple
import urllib3

# Set UTF-8 encoding for stdout/stderr to avoid charmap errors on Windows
if sys.platform == 'win32':
    import codecs
    # Wrap stdout/stderr with UTF-8 codec to prevent charmap errors
    if hasattr(sys.stdout, 'buffer'):
        sys.stdout = codecs.getwriter('utf-8')(sys.stdout.buffer, 'replace')
    if hasattr(sys.stderr, 'buffer'):
        sys.stderr = codecs.getwriter('utf-8')(sys.stderr.buffer, 'replace')
    
    # Set environment variable for Python to use UTF-8
    os.environ['PYTHONIOENCODING'] = 'utf-8'

# Disable SSL warnings for insecure connections
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Configure logging with UTF-8 encoding
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('mta_analyzer.log', encoding='utf-8', errors='replace')
    ]
)
logger = logging.getLogger(__name__)


class MTAAnalyzer:
    """Main class for MTA analysis automation"""
    
    def __init__(self, mta_url: str):
        """
        Initialize MTA Analyzer
        
        Args:
            mta_url: Base URL of MTA installation (e.g., https://mta.example.com)
        """
        self.mta_url = mta_url.rstrip('/')
        self.temp_dir = None
        
    def read_input_csv(self, csv_path: str) -> List[Dict[str, str]]:
        """
        Read input CSV file with app_name and image_url
        
        Args:
            csv_path: Path to input CSV file
            
        Returns:
            List of dictionaries with app_name and image_url
        """
        logger.info(f"Reading input CSV: {csv_path}")
        applications = []
        
        try:
            with open(csv_path, 'r', encoding='utf-8') as csvfile:
                reader = csv.DictReader(csvfile)
                for row in reader:
                    if 'app_name' in row and 'image_url' in row:
                        applications.append({
                            'app_name': row['app_name'].strip(),
                            'image_url': row['image_url'].strip()
                        })
                    else:
                        logger.warning(f"Skipping invalid row: {row}")
        except Exception as e:
            logger.error(f"Error reading CSV file: {e}")
            raise
            
        logger.info(f"Found {len(applications)} applications to analyze")
        return applications
    
    def pull_docker_image(self, image_url: str) -> bool:
        """
        Pull Docker image
        
        Args:
            image_url: Docker image URL
            
        Returns:
            True if successful, False otherwise
        """
        logger.info(f"Pulling Docker image: {image_url}")
        
        try:
            result = subprocess.run(
                ['docker', 'pull', image_url],
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                check=True
            )
            logger.info(f"Successfully pulled image: {image_url}")
            return True
        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to pull image {image_url}: {e.stderr}")
            return False
        except FileNotFoundError:
            logger.error("Docker command not found. Please ensure Docker is installed.")
            return False
    
    def extract_artifacts_from_image(self, image_url: str, app_name: str) -> Optional[str]:
        """
        Extract JAR/WAR files from Docker image
        
        Args:
            image_url: Docker image URL
            app_name: Application name
            
        Returns:
            Path to extracted artifact or None if not found
        """
        logger.info(f"Extracting artifacts from image: {image_url}")
        
        # Create temporary directory for extraction
        if not self.temp_dir:
            self.temp_dir = tempfile.mkdtemp(prefix='mta_')
        
        app_temp_dir = os.path.join(self.temp_dir, app_name)
        os.makedirs(app_temp_dir, exist_ok=True)
        
        artifact_path = None
        container_id = None
        
        try:
            # Create a temporary container
            logger.info("Creating temporary container...")
            result = subprocess.run(
                ['docker', 'create', image_url],
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                check=True
            )
            container_id = result.stdout.strip()
            logger.info(f"Created container: {container_id}")
            
            # Copy entire filesystem to temp directory for searching
            logger.info("Copying container filesystem for analysis...")
            container_export_path = os.path.join(app_temp_dir, 'container_fs')
            os.makedirs(container_export_path, exist_ok=True)
            
            # Export container filesystem as tar and extract
            export_result = subprocess.run(
                ['docker', 'export', container_id],
                capture_output=True,
                check=True
            )
            
            # Extract tar to temp directory
            tar_data = io.BytesIO(export_result.stdout)
            with tarfile.open(fileobj=tar_data, mode='r') as tar:
                # Extract with error handling for Windows path issues
                def safe_extract(tarinfo, path):
                    """Safely extract tar members, handling Windows path limitations"""
                    try:
                        # Skip problematic files on Windows (device files, symlinks, etc.)
                        if sys.platform == 'win32':
                            if tarinfo.isdev() or tarinfo.issym() or tarinfo.islnk():
                                logger.debug(f"Skipping special file: {tarinfo.name}")
                                return
                            # Sanitize path for Windows
                            tarinfo.name = tarinfo.name.replace(':', '_').replace('|', '_')
                        tar.extract(tarinfo, path)
                    except Exception as e:
                        logger.debug(f"Could not extract {tarinfo.name}: {e}")
                
                for member in tar.getmembers():
                    safe_extract(member, container_export_path)
            
            logger.info("Container filesystem extracted, searching for artifacts...")
            
            # Search for JAR/WAR files
            found_files = []
            for root, dirs, files in os.walk(container_export_path):
                for file in files:
                    if file.endswith('.jar') or file.endswith('.war'):
                        full_path = os.path.join(root, file)
                        rel_path = os.path.relpath(full_path, container_export_path)
                        found_files.append((full_path, rel_path))
            
            logger.info(f"Found {len(found_files)} JAR/WAR files")
            
            if found_files:
                # Filter out library JARs
                app_files = [
                    (full, rel) for full, rel in found_files
                    if not any(lib in rel.lower() for lib in [
                        '/lib/', '/libs/', '/library/', '/libraries/',
                        'maven', 'gradle', '.m2', '/jre/', '/jdk/'
                    ])
                ]
                
                logger.info(f"After filtering libraries: {len(app_files)} application artifacts")
                
                if app_files:
                    # Prefer files in specific locations or with specific names
                    preferred_paths = ['/opt/app', '/app', '/deployments', '/usr/local/app']
                    
                    # First, try to find app.jar or app.war
                    for full_path, rel_path in app_files:
                        if 'app.jar' in rel_path.lower() or 'app.war' in rel_path.lower():
                            artifact_path = full_path
                            logger.info(f"Found preferred artifact: {rel_path}")
                            break
                    
                    # Second, try files in preferred paths
                    if not artifact_path:
                        for full_path, rel_path in app_files:
                            if any(pref in rel_path for pref in preferred_paths):
                                artifact_path = full_path
                                logger.info(f"Found artifact in preferred path: {rel_path}")
                                break
                    
                    # Third, try files with app name
                    if not artifact_path:
                        for full_path, rel_path in app_files:
                            if app_name.lower() in rel_path.lower():
                                artifact_path = full_path
                                logger.info(f"Found artifact matching app name: {rel_path}")
                                break
                    
                    # Finally, use the first non-library file
                    if not artifact_path:
                        artifact_path = app_files[0][0]
                        logger.info(f"Using first available artifact: {app_files[0][1]}")
                    
                    # Copy to final location with standard name
                    local_artifact_name = 'app' + os.path.splitext(artifact_path)[1]
                    local_artifact_path = os.path.join(app_temp_dir, local_artifact_name)
                    shutil.copy2(artifact_path, local_artifact_path)
                    
                    logger.info(f"Extracted artifact to: {local_artifact_path}")
                    
                    # Cleanup container filesystem
                    shutil.rmtree(container_export_path)
                    
                    return local_artifact_path
                else:
                    logger.warning(f"No application artifacts found after filtering (only libraries)")
                    # Log all found files for debugging
                    for full_path, rel_path in found_files:
                        logger.debug(f"  Found (filtered out): {rel_path}")
            else:
                logger.warning(f"No JAR/WAR files found in image: {image_url}")
            
            return None
                    
        except subprocess.CalledProcessError as e:
            logger.error(f"Error extracting artifacts: {e.stderr if e.stderr else str(e)}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error during artifact extraction: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return None
        finally:
            # Remove the temporary container
            if container_id:
                subprocess.run(
                    ['docker', 'rm', container_id],
                    capture_output=True,
                    encoding='utf-8',
                    errors='replace'
                )
                logger.info(f"Removed temporary container: {container_id}")
    
    def curl_request(self, method: str, url: str, data: Optional[str] = None, 
                     file_path: Optional[str] = None) -> Tuple[int, str]:
        """
        Make HTTP request using curl command (for insecure SSL)
        
        Args:
            method: HTTP method (GET, POST, PUT, etc.)
            url: Full URL
            data: JSON data for POST/PUT requests
            file_path: File path for file upload
            
        Returns:
            Tuple of (status_code, response_body)
        """
        cmd = ['curl', '-k', '-s', '-w', '\\n%{http_code}', '-X', method]
        
        if data:
            cmd.extend(['-H', 'Content-Type: application/json', '-d', data])
        
        if file_path:
            cmd.extend(['-H', 'Content-Type: application/octet-stream',
                       '--data-binary', f'@{file_path}'])
        
        cmd.append(url)
        
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                check=False
            )
            output = result.stdout
            
            # Split output and status code
            lines = output.rsplit('\n', 1)
            if len(lines) == 2:
                response_body = lines[0]
                status_code = int(lines[1])
            else:
                response_body = output
                status_code = 0
            
            return status_code, response_body
        except Exception as e:
            logger.error(f"Curl request failed: {e}")
            return 0, str(e)
    
    def create_application(self, app_name: str) -> Optional[int]:
        """
        Create a new application in MTA
        
        Args:
            app_name: Application name
            
        Returns:
            Application ID or None if failed
        """
        logger.info(f"Creating application: {app_name}")
        
        url = f"{self.mta_url}/hub/applications"
        data = json.dumps({"name": app_name})
        
        status_code, response = self.curl_request('POST', url, data=data)
        
        if status_code in [200, 201]:
            try:
                app_data = json.loads(response)
                app_id = app_data.get('id')
                logger.info(f"Created application with ID: {app_id}")
                return app_id
            except json.JSONDecodeError:
                logger.error(f"Failed to parse application creation response: {response}")
                return None
        else:
            logger.error(f"Failed to create application. Status: {status_code}, Response: {response}")
            return None
    
    def create_task(self, app_id: int, artifact_name: str) -> Optional[int]:
        """
        Create a new analysis task in MTA
        
        Args:
            app_id: Application ID
            artifact_name: Name of the artifact file
            
        Returns:
            Task ID or None if failed
        """
        logger.info(f"Creating analysis task for application ID: {app_id}")
        
        url = f"{self.mta_url}/hub/tasks"
        
        task_data = {
            "name": f"{app_id}-Analyse",
            "application": {"id": app_id},
            "kind": "analyzer",
            "addon": "analyzer",
            "data": {
                "mode": {
                    "binary": True,
                    "artifact": f"/binary/{artifact_name}"
                },
                "targets": ["cloud-readiness"],
                "sources": []
            }
        }
        
        data = json.dumps(task_data)
        status_code, response = self.curl_request('POST', url, data=data)
        
        if status_code in [200, 201]:
            try:
                task_data = json.loads(response)
                task_id = task_data.get('id')
                logger.info(f"Created task with ID: {task_id}")
                return task_id
            except json.JSONDecodeError:
                logger.error(f"Failed to parse task creation response: {response}")
                return None
        else:
            logger.error(f"Failed to create task. Status: {status_code}, Response: {response}")
            return None
    
    def upload_artifact(self, task_id: int, artifact_path: str) -> bool:
        """
        Upload artifact to task bucket
        
        Args:
            task_id: Task ID
            artifact_path: Local path to artifact file
            
        Returns:
            True if successful, False otherwise
        """
        logger.info(f"Uploading artifact to task {task_id}")
        
        artifact_name = os.path.basename(artifact_path)
        # Ensure the artifact is named app.jar or app.war
        if not artifact_name.startswith('app.'):
            artifact_name = 'app' + os.path.splitext(artifact_name)[1]
        
        url = f"{self.mta_url}/hub/tasks/{task_id}/bucket/binary/{artifact_name}"
        
        status_code, response = self.curl_request('PUT', url, file_path=artifact_path)
        
        if status_code in [200, 201, 204]:
            logger.info(f"Successfully uploaded artifact for task {task_id}")
            return True
        else:
            logger.error(f"Failed to upload artifact. Status: {status_code}, Response: {response}")
            return False
    
    def submit_task(self, task_id: int) -> bool:
        """
        Submit analysis task for execution
        
        Args:
            task_id: Task ID
            
        Returns:
            True if successful, False otherwise
        """
        logger.info(f"Submitting task {task_id}")
        
        url = f"{self.mta_url}/hub/tasks/{task_id}/submit"
        data = json.dumps({"state": "ready"})
        
        status_code, response = self.curl_request('PUT', url, data=data)
        
        if status_code in [200, 201, 204]:
            logger.info(f"Successfully submitted task {task_id}")
            return True
        else:
            logger.error(f"Failed to submit task. Status: {status_code}, Response: {response}")
            return False
    
    def process_application(self, app_name: str, image_url: str) -> bool:
        """
        Process a single application through the complete MTA workflow
        
        Args:
            app_name: Application name
            image_url: Docker image URL
            
        Returns:
            True if successful, False otherwise
        """
        logger.info(f"\n{'='*60}")
        logger.info(f"Processing application: {app_name}")
        logger.info(f"Image: {image_url}")
        logger.info(f"{'='*60}\n")
        
        # Step 1: Pull Docker image
        if not self.pull_docker_image(image_url):
            logger.error(f"Failed to pull image for {app_name}")
            return False
        
        # Step 2: Extract artifacts
        artifact_path = self.extract_artifacts_from_image(image_url, app_name)
        if not artifact_path:
            logger.error(f"Failed to extract artifacts for {app_name}")
            return False
        
        artifact_name = os.path.basename(artifact_path)
        
        # Step 3: Create application in MTA
        app_id = self.create_application(app_name)
        if not app_id:
            logger.error(f"Failed to create application for {app_name}")
            return False
        
        # Step 4: Create analysis task
        task_id = self.create_task(app_id, artifact_name)
        if not task_id:
            logger.error(f"Failed to create task for {app_name}")
            return False
        
        # Step 5: Upload artifact
        if not self.upload_artifact(task_id, artifact_path):
            logger.error(f"Failed to upload artifact for {app_name}")
            return False
        
        # Step 6: Submit task
        if not self.submit_task(task_id):
            logger.error(f"Failed to submit task for {app_name}")
            return False
        
        logger.info(f"\n✓ Successfully processed {app_name}")
        logger.info(f"  Application ID: {app_id}")
        logger.info(f"  Task ID: {task_id}\n")
        
        return True
    
    def process_all(self, csv_path: str) -> Dict[str, bool]:
        """
        Process all applications from CSV file
        
        Args:
            csv_path: Path to input CSV file
            
        Returns:
            Dictionary mapping app_name to success status
        """
        results = {}
        
        try:
            applications = self.read_input_csv(csv_path)
            
            if not applications:
                logger.warning("No applications found in CSV file")
                return results
            
            for app in applications:
                app_name = app['app_name']
                image_url = app['image_url']
                
                try:
                    success = self.process_application(app_name, image_url)
                    results[app_name] = success
                except Exception as e:
                    logger.error(f"Error processing {app_name}: {e}")
                    results[app_name] = False
            
            # Summary
            logger.info(f"\n{'='*60}")
            logger.info("PROCESSING SUMMARY")
            logger.info(f"{'='*60}")
            successful = sum(1 for v in results.values() if v)
            failed = len(results) - successful
            logger.info(f"Total applications: {len(results)}")
            logger.info(f"Successful: {successful}")
            logger.info(f"Failed: {failed}")
            
            for app_name, success in results.items():
                status = "✓ SUCCESS" if success else "✗ FAILED"
                logger.info(f"  {app_name}: {status}")
            
        finally:
            # Cleanup temporary directory
            if self.temp_dir and os.path.exists(self.temp_dir):
                shutil.rmtree(self.temp_dir)
                logger.info(f"\nCleaned up temporary directory: {self.temp_dir}")
        
        return results


def main():
    """Main entry point"""
    if len(sys.argv) < 3:
        print("Usage: python mta_analyzer.py <mta_url> <input_csv>")
        print("Example: python mta_analyzer.py https://mta.example.com input.csv")
        sys.exit(1)
    
    mta_url = sys.argv[1]
    csv_path = sys.argv[2]
    
    if not os.path.exists(csv_path):
        logger.error(f"Input CSV file not found: {csv_path}")
        sys.exit(1)
    
    logger.info("Starting MTA Analyzer")
    logger.info(f"MTA URL: {mta_url}")
    logger.info(f"Input CSV: {csv_path}")
    
    analyzer = MTAAnalyzer(mta_url)
    results = analyzer.process_all(csv_path)
    
    # Exit with error code if any application failed
    if any(not success for success in results.values()):
        sys.exit(1)
    
    logger.info("\nMTA Analyzer completed successfully")


if __name__ == '__main__':
    main()

# Made with Bob
