#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MTA (Migration Toolkit for Applications) Analyzer
Automates the process of analyzing applications using Red Hat MTA 8.0.1
"""

import argparse
import csv
import json
import logging
import os
import subprocess
import sys
import tempfile
import shutil
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
    
    def __init__(self, mta_url: str, output_dir: str = 'artifacts'):
        """
        Initialize MTA Analyzer
        
        Args:
            mta_url: Base URL of MTA installation (e.g., https://mta.example.com)
            output_dir: Directory to store extracted artifacts
        """
        self.mta_url = mta_url.rstrip('/')
        self.output_dir = output_dir
        os.makedirs(output_dir, exist_ok=True)
        
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
        Extract JAR/WAR files from Docker image by creating a container and searching within it
        
        Args:
            image_url: Docker image URL
            app_name: Application name
            
        Returns:
            Path to extracted artifact or None if not found
        """
        logger.info(f"Extracting artifacts from image: {image_url}")
        
        # Create app-specific directory
        app_dir = os.path.join(self.output_dir, app_name)
        os.makedirs(app_dir, exist_ok=True)
        
        container_id = None
        artifact_path = None
        
        try:
            # Create a container (not running)
            logger.info("Creating container...")
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
            
            # Common paths where application artifacts might be located
            search_paths = [
                '/opt/app',
                '/app',
                '/usr/local/app',
                '/deployments',
                '/opt',
                '/usr/src/app',
                '/home',
                '/'
            ]
            
            found_artifacts = []
            
            # Search for JAR/WAR files in each path
            for search_path in search_paths:
                logger.info(f"Searching in {search_path}...")
                
                # Use docker exec with find command to search for artifacts
                # We use sh -c to handle the find command properly
                find_cmd = f"find {search_path} -maxdepth 5 -type f \\( -name '*.jar' -o -name '*.war' \\) 2>/dev/null"
                
                result = subprocess.run(
                    ['docker', 'run', '--rm', '--entrypoint', 'sh', image_url, '-c', find_cmd],
                    capture_output=True,
                    text=True,
                    encoding='utf-8',
                    errors='replace',
                    timeout=30
                )
                
                if result.returncode == 0 and result.stdout.strip():
                    files = result.stdout.strip().split('\n')
                    for f in files:
                        if f.strip():
                            found_artifacts.append(f.strip())
                    
                    if found_artifacts:
                        logger.info(f"Found {len(found_artifacts)} artifacts in {search_path}")
                        break
            
            if not found_artifacts:
                logger.warning(f"No JAR/WAR files found in image: {image_url}")
                return None
            
            logger.info(f"Total artifacts found: {len(found_artifacts)}")
            
            # Filter out library JARs
            app_files = [
                f for f in found_artifacts
                if not any(lib in f.lower() for lib in [
                    '/lib/', '/libs/', '/library/', '/libraries/',
                    'maven', 'gradle', '.m2', '/jre/', '/jdk/',
                    'tomcat', 'catalina', 'spring-boot-loader'
                ])
            ]
            
            logger.info(f"After filtering libraries: {len(app_files)} application artifacts")
            
            if not app_files:
                logger.warning("No application artifacts found after filtering (only libraries)")
                # Log what was filtered out for debugging
                for f in found_artifacts[:5]:  # Show first 5
                    logger.debug(f"  Filtered out: {f}")
                return None
            
            # Select the best artifact
            selected_artifact = None
            
            # Priority 1: Files named app.jar or app.war
            for f in app_files:
                if 'app.jar' in f.lower() or 'app.war' in f.lower():
                    selected_artifact = f
                    logger.info(f"Selected preferred artifact: {f}")
                    break
            
            # Priority 2: Files in preferred paths
            if not selected_artifact:
                preferred_paths = ['/opt/app', '/app', '/deployments', '/usr/local/app']
                for f in app_files:
                    if any(pref in f for pref in preferred_paths):
                        selected_artifact = f
                        logger.info(f"Selected artifact from preferred path: {f}")
                        break
            
            # Priority 3: Files matching app name
            if not selected_artifact:
                for f in app_files:
                    if app_name.lower() in f.lower():
                        selected_artifact = f
                        logger.info(f"Selected artifact matching app name: {f}")
                        break
            
            # Priority 4: First available artifact
            if not selected_artifact:
                selected_artifact = app_files[0]
                logger.info(f"Using first available artifact: {selected_artifact}")
            
            # Copy the artifact from container
            file_extension = os.path.splitext(selected_artifact)[1]
            local_artifact_name = f'app{file_extension}'
            local_artifact_path = os.path.join(app_dir, local_artifact_name)
            
            logger.info(f"Copying artifact from container...")
            copy_result = subprocess.run(
                ['docker', 'cp', f'{container_id}:{selected_artifact}', local_artifact_path],
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                check=True
            )
            
            # Verify file was copied
            if os.path.exists(local_artifact_path):
                file_size = os.path.getsize(local_artifact_path)
                logger.info(f"Successfully extracted artifact to: {local_artifact_path} ({file_size} bytes)")
                return local_artifact_path
            else:
                logger.error(f"Failed to copy artifact to {local_artifact_path}")
                return None
                    
        except subprocess.TimeoutExpired:
            logger.error("Timeout while searching for artifacts in container")
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
            # Remove the container
            if container_id:
                subprocess.run(
                    ['docker', 'rm', '-f', container_id],
                    capture_output=True,
                    encoding='utf-8',
                    errors='replace'
                )
                logger.info(f"Removed container: {container_id}")
    
    def curl_request(self, method: str, url: str, data: Optional[str] = None,
                     file_path: Optional[str] = None, file_field_name: str = 'file') -> Tuple[int, str]:
        """
        Make HTTP request using curl command (for insecure SSL)
        
        Args:
            method: HTTP method (GET, POST, PUT, etc.)
            url: Full URL
            data: JSON data for POST/PUT requests
            file_path: File path for file upload
            file_field_name: Form field name for file upload (default: 'file')
            
        Returns:
            Tuple of (status_code, response_body)
        """
        cmd = ['curl', '-k', '-s', '-w', '\\n%{http_code}', '-X', method]
        
        if data:
            cmd.extend(['-H', 'Content-Type: application/json', '-d', data])
        
        if file_path:
            # Use multipart/form-data for file uploads
            # curl automatically sets Content-Type to multipart/form-data when using -F
            cmd.extend(['-F', f'{file_field_name}=@{file_path}'])
        
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
        Upload artifact to task bucket with streaming for memory efficiency
        
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
        
        # Get file size for logging
        file_size = os.path.getsize(artifact_path)
        file_size_mb = file_size / (1024 * 1024)
        logger.info(f"Uploading {artifact_name} ({file_size_mb:.2f} MB)")
        
        # curl with --data-binary streams the file efficiently without loading into memory
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
        logger.info(f"  Task ID: {task_id}")
        logger.info(f"  Artifact saved: {artifact_path}\n")
        
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
            logger.info(f"Artifacts directory: {os.path.abspath(self.output_dir)}")
            
            for app_name, success in results.items():
                status = "✓ SUCCESS" if success else "✗ FAILED"
                logger.info(f"  {app_name}: {status}")
            
        except Exception as e:
            logger.error(f"Error in process_all: {e}")
            import traceback
            logger.error(traceback.format_exc())
        
        return results


def parse_arguments():
    """Parse command line arguments"""
    parser = argparse.ArgumentParser(
        description='MTA (Migration Toolkit for Applications) Analyzer - Automate application analysis',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
Examples:
  # Basic usage
  python mta_analyzer.py https://mta.example.com input.csv
  
  # Specify custom output directory
  python mta_analyzer.py https://mta.example.com input.csv --output-dir /path/to/artifacts
  
  # Enable debug logging
  python mta_analyzer.py https://mta.example.com input.csv --debug
  
  # Show version
  python mta_analyzer.py --version
        '''
    )
    
    parser.add_argument(
        'mta_url',
        help='Base URL of MTA installation (e.g., https://mta.example.com)'
    )
    
    parser.add_argument(
        'input_csv',
        help='Path to input CSV file with app_name and image_url columns'
    )
    
    parser.add_argument(
        '-o', '--output-dir',
        default='artifacts',
        help='Directory to store extracted artifacts (default: artifacts)'
    )
    
    parser.add_argument(
        '-d', '--debug',
        action='store_true',
        help='Enable debug logging'
    )
    
    parser.add_argument(
        '-v', '--version',
        action='version',
        version='MTA Analyzer 1.0.0'
    )
    
    return parser.parse_args()


def main():
    """Main entry point"""
    args = parse_arguments()
    
    # Set debug logging if requested
    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)
        logger.debug("Debug logging enabled")
    
    # Validate input CSV exists
    if not os.path.exists(args.input_csv):
        logger.error(f"Input CSV file not found: {args.input_csv}")
        sys.exit(1)
    
    logger.info("="*60)
    logger.info("MTA Analyzer v1.0.0")
    logger.info("="*60)
    logger.info(f"MTA URL: {args.mta_url}")
    logger.info(f"Input CSV: {args.input_csv}")
    logger.info(f"Output Directory: {args.output_dir}")
    logger.info("="*60)
    
    try:
        analyzer = MTAAnalyzer(args.mta_url, output_dir=args.output_dir)
        results = analyzer.process_all(args.input_csv)
        
        # Exit with error code if any application failed
        if any(not success for success in results.values()):
            logger.error("\nSome applications failed to process")
            sys.exit(1)
        
        logger.info("\n" + "="*60)
        logger.info("MTA Analyzer completed successfully!")
        logger.info("="*60)
        
    except KeyboardInterrupt:
        logger.warning("\n\nProcess interrupted by user")
        sys.exit(130)
    except Exception as e:
        logger.error(f"\nFatal error: {e}")
        import traceback
        logger.error(traceback.format_exc())
        sys.exit(1)


if __name__ == '__main__':
    main()

# Made with Bob
