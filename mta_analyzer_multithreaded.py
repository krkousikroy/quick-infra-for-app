#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MTA (Migration Toolkit for Applications) Analyzer - Multi-threaded Version
Automates the process of analyzing applications using Red Hat MTA 8.0.1 with parallel processing
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
from typing import List, Dict, Optional, Tuple, Any
import urllib3
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
import time

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

# Thread-safe logging setup
class ThreadSafeLogger:
    """Thread-safe logger wrapper"""
    def __init__(self, logger):
        self.logger = logger
        self.lock = Lock()
    
    def _log(self, level, msg, *args, **kwargs):
        with self.lock:
            getattr(self.logger, level)(msg, *args, **kwargs)
    
    def info(self, msg, *args, **kwargs):
        self._log('info', msg, *args, **kwargs)
    
    def warning(self, msg, *args, **kwargs):
        self._log('warning', msg, *args, **kwargs)
    
    def error(self, msg, *args, **kwargs):
        self._log('error', msg, *args, **kwargs)
    
    def debug(self, msg, *args, **kwargs):
        self._log('debug', msg, *args, **kwargs)

# Configure logging with UTF-8 encoding
base_logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - [%(threadName)-10s] - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('mta_analyzer.log', encoding='utf-8', errors='replace')
    ]
)
logger = ThreadSafeLogger(base_logger)


class MTAAnalyzer:
    """Main class for MTA analysis automation with multi-threading support"""
    
    def __init__(self, mta_url: str, output_dir: str = 'artifacts', cleanup: bool = True, max_workers: int = 4):
        """
        Initialize MTA Analyzer
        
        Args:
            mta_url: Base URL of MTA installation (e.g., https://mta.example.com)
            output_dir: Directory to store extracted artifacts
            cleanup: Whether to cleanup Docker images/containers and artifacts after processing
            max_workers: Maximum number of concurrent threads (default: 4)
        """
        self.mta_url = mta_url.rstrip('/')
        self.output_dir = output_dir
        self.cleanup = cleanup
        self.max_workers = max_workers
        self.processed_images = []  # Track images for cleanup
        self.images_lock = Lock()  # Thread-safe access to processed_images
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
        Pull Docker image (thread-safe)
        
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
            # Track image for cleanup (thread-safe)
            with self.images_lock:
                if image_url not in self.processed_images:
                    self.processed_images.append(image_url)
            return True
        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to pull image {image_url}: {e.stderr}")
            return False
        except FileNotFoundError:
            logger.error("Docker command not found. Please ensure Docker is installed.")
            return False
    
    def cleanup_docker_image(self, image_url: str) -> bool:
        """
        Remove Docker image from local machine
        
        Args:
            image_url: Docker image URL to remove
            
        Returns:
            True if successful, False otherwise
        """
        logger.info(f"Cleaning up Docker image: {image_url}")
        
        try:
            result = subprocess.run(
                ['docker', 'rmi', '-f', image_url],
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                check=True
            )
            logger.info(f"Successfully removed image: {image_url}")
            return True
        except subprocess.CalledProcessError as e:
            logger.warning(f"Failed to remove image {image_url}: {e.stderr}")
            return False
        except FileNotFoundError:
            logger.error("Docker command not found.")
            return False
    
    def cleanup_artifacts(self, app_name: str) -> bool:
        """
        Remove extracted artifacts for an application
        
        Args:
            app_name: Application name
            
        Returns:
            True if successful, False otherwise
        """
        app_dir = os.path.join(self.output_dir, app_name)
        
        if not os.path.exists(app_dir):
            logger.debug(f"Artifact directory does not exist: {app_dir}")
            return True
        
        try:
            shutil.rmtree(app_dir)
            logger.info(f"Cleaned up artifacts for {app_name}: {app_dir}")
            return True
        except Exception as e:
            logger.warning(f"Failed to cleanup artifacts for {app_name}: {e}")
            return False
    
    def cleanup_all_docker_images(self) -> None:
        """
        Clean up all Docker images that were pulled during processing
        """
        with self.images_lock:
            if not self.processed_images:
                logger.debug("No Docker images to clean up")
                return
            
            logger.info(f"\nCleaning up {len(self.processed_images)} Docker images...")
            
            for image_url in self.processed_images:
                self.cleanup_docker_image(image_url)
            
            self.processed_images.clear()
    
    def get_docker_image_metadata(self, image_url: str) -> Dict[str, Any]:
        """
        Extract metadata from Docker image (ENTRYPOINT, CMD, ENV, LABELS)
        
        Args:
            image_url: Docker image URL
            
        Returns:
            Dictionary with image metadata
        """
        metadata = {
            'entrypoint': [],
            'cmd': [],
            'env': {},
            'labels': {},
            'workdir': ''
        }
        
        try:
            # Inspect the Docker image
            result = subprocess.run(
                ['docker', 'inspect', image_url],
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                timeout=10
            )
            
            if result.returncode == 0:
                inspect_data = json.loads(result.stdout)
                
                if inspect_data and len(inspect_data) > 0:
                    config = inspect_data[0].get('Config', {})
                    
                    # Extract ENTRYPOINT
                    entrypoint = config.get('Entrypoint') or []
                    metadata['entrypoint'] = entrypoint if isinstance(entrypoint, list) else [entrypoint]
                    
                    # Extract CMD
                    cmd = config.get('Cmd') or []
                    metadata['cmd'] = cmd if isinstance(cmd, list) else [cmd]
                    
                    # Extract ENV variables
                    env_list = config.get('Env') or []
                    for env_var in env_list:
                        if '=' in env_var:
                            key, value = env_var.split('=', 1)
                            metadata['env'][key] = value
                    
                    # Extract Labels
                    metadata['labels'] = config.get('Labels') or {}
                    
                    # Extract WorkingDir
                    metadata['workdir'] = config.get('WorkingDir', '')
                    
                    logger.info(f"Extracted image metadata: ENTRYPOINT={metadata['entrypoint']}, CMD={metadata['cmd']}")
                    
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError, json.JSONDecodeError) as e:
            logger.warning(f"Could not extract image metadata: {e}")
        
        return metadata
    
    def extract_artifact_hints_from_metadata(self, metadata: Dict[str, Any]) -> List[str]:
        """
        Extract potential artifact paths from Docker image metadata
        
        Args:
            metadata: Docker image metadata
            
        Returns:
            List of potential artifact paths mentioned in ENTRYPOINT/CMD
        """
        hints = []
        
        # Combine ENTRYPOINT and CMD
        all_commands = metadata.get('entrypoint', []) + metadata.get('cmd', [])
        
        for cmd_part in all_commands:
            if isinstance(cmd_part, str):
                # Look for .jar or .war references
                if '.jar' in cmd_part.lower() or '.war' in cmd_part.lower():
                    # Extract potential file paths
                    parts = cmd_part.split()
                    for part in parts:
                        if part.endswith('.jar') or part.endswith('.war'):
                            # Clean up the path (remove flags, quotes, etc.)
                            clean_path = part.strip('"\'')
                            if clean_path.startswith('/') or clean_path.startswith('./'):
                                hints.append(clean_path)
                                logger.info(f"Found artifact hint from metadata: {clean_path}")
        
        # Check environment variables for common deployment paths
        env_vars = metadata.get('env', {})
        for key, value in env_vars.items():
            if any(keyword in key.upper() for keyword in ['DEPLOY', 'APP', 'JAR', 'WAR', 'ARTIFACT']):
                if '.jar' in value.lower() or '.war' in value.lower():
                    hints.append(value)
                    logger.info(f"Found artifact hint from env var {key}: {value}")
        
        return hints
    
    def get_file_size_in_container(self, container_id: str, filepath: str) -> int:
        """
        Get file size of a file in a Docker container
        
        Args:
            container_id: Docker container ID
            filepath: Path to file in container
            
        Returns:
            File size in bytes, or 0 if unable to determine
        """
        try:
            # Use stat command to get file size
            result = subprocess.run(
                ['docker', 'exec', container_id, 'stat', '-c', '%s', filepath],
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                timeout=5
            )
            if result.returncode == 0:
                return int(result.stdout.strip())
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError, ValueError):
            pass
        return 0
    
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
        
        # Extract metadata from Docker image for hints
        image_metadata = self.get_docker_image_metadata(image_url)
        artifact_hints = self.extract_artifact_hints_from_metadata(image_metadata)
        
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
            
            # Enhanced filtering: Filter out library JARs and common non-application files
            app_files = [
                f for f in found_artifacts
                if not any(lib in f.lower() for lib in [
                    '/lib/', '/libs/', '/library/', '/libraries/',
                    'maven', 'gradle', '.m2', '/jre/', '/jdk/',
                    'tomcat', 'catalina', 'spring-boot-loader',
                    '/endorsed/', '/ext/', '/classes/',
                    'junit', 'mockito', 'hamcrest', 'test',
                    'servlet-api', 'jsp-api', 'el-api'
                ])
            ]
            
            logger.info(f"After filtering libraries: {len(app_files)} application artifacts")
            
            if not app_files:
                logger.warning("No application artifacts found after filtering (only libraries)")
                for f in found_artifacts[:5]:
                    logger.debug(f"  Filtered out: {f}")
                return None
            
            # Log all candidate artifacts for transparency
            if len(app_files) > 1:
                logger.info(f"Multiple artifacts found ({len(app_files)}), applying selection logic:")
                for idx, f in enumerate(app_files, 1):
                    logger.info(f"  Candidate {idx}: {f}")
            
            # Enhanced selection logic with advanced scoring system
            def score_artifact(filepath: str, app_name: str, file_size: int = 0,
                             metadata_hints: Optional[List[str]] = None) -> tuple:
                """
                Score an artifact based on multiple criteria.
                Returns tuple: (score, priority_level, filepath, details_dict)
                Higher score = better match
                """
                score = 0
                priority = 0
                details = {}
                filename = os.path.basename(filepath).lower()
                filepath_lower = filepath.lower()
                metadata_hints = metadata_hints or []
                
                # Priority 0: Exact match from Docker metadata (ENTRYPOINT/CMD)
                for hint in metadata_hints:
                    if filepath in hint or hint in filepath:
                        score += 1500
                        priority = 0
                        details['metadata_match'] = True
                        details['metadata_hint'] = hint
                        logger.info(f"Artifact matches Docker metadata hint: {hint}")
                        break
                
                # Priority 1: Exact name matches (highest priority)
                if filename == 'app.jar' or filename == 'app.war':
                    score += 1000
                    priority = 1 if priority == 0 else priority
                    details['exact_match'] = True
                elif filename == 'application.jar' or filename == 'application.war':
                    score += 900
                    priority = 1 if priority == 0 else priority
                    details['exact_match'] = True
                
                # Priority 2: App name match in filename
                if app_name.lower() in filename:
                    score += 800
                    priority = 2 if priority == 0 else priority
                    details['app_name_match'] = True
                
                # Priority 3: Red Hat EAP/JBoss specific paths and patterns
                # Red Hat EAP/JBoss deployment directories
                eap_jboss_paths = [
                    '/opt/eap/standalone/deployments/',
                    '/opt/jboss/standalone/deployments/',
                    '/opt/wildfly/standalone/deployments/',
                    '/deployments/',
                    '/standalone/deployments/',
                    '/domain/deployments/',
                    '/opt/eap/',
                    '/opt/jboss/',
                    '/opt/wildfly/'
                ]
                for eap_path in eap_jboss_paths:
                    if eap_path in filepath_lower:
                        score += 850
                        if priority == 0:
                            priority = 3
                        details['eap_jboss_path'] = eap_path
                        logger.info(f"Detected Red Hat EAP/JBoss deployment path: {eap_path}")
                        break
                
                # Priority 4: Preferred deployment paths (general)
                preferred_paths = ['/opt/app/', '/app/', '/usr/local/app/']
                for pref_path in preferred_paths:
                    if pref_path in filepath_lower:
                        score += 700
                        if priority == 0:
                            priority = 4
                        details['preferred_path'] = pref_path
                        break
                
                # Priority 5: Root-level artifacts (often main application)
                path_depth = filepath.count('/')
                if path_depth <= 2:  # Root or one level deep
                    score += 600
                    if priority == 0:
                        priority = 5
                    details['root_level'] = True
                
                # Red Hat EAP/JBoss specific file patterns
                eap_jboss_patterns = ['jboss', 'eap', 'wildfly', 'redhat']
                for pattern in eap_jboss_patterns:
                    if pattern in filename:
                        score += 300
                        details['eap_jboss_pattern'] = pattern
                        logger.info(f"Detected Red Hat EAP/JBoss pattern in filename: {pattern}")
                        break
                
                # Priority 6: File size analysis (larger files often main apps)
                if file_size > 0:
                    size_mb = file_size / (1024 * 1024)
                    details['size_mb'] = round(size_mb, 2)
                    
                    # Main applications typically 1MB - 500MB
                    if 1 <= size_mb <= 500:
                        score += min(int(size_mb * 2), 500)  # Up to 500 points
                        details['size_category'] = 'main_app'
                    elif size_mb > 500:
                        score += 400  # Large but cap the bonus
                        details['size_category'] = 'large_app'
                    elif 0.1 <= size_mb < 1:
                        score += 100  # Small utility
                        details['size_category'] = 'utility'
                    else:
                        score += 50  # Very small, likely config
                        details['size_category'] = 'tiny'
                
                # Bonus points for WAR files (often main web applications)
                if filename.endswith('.war'):
                    score += 150
                    details['is_war'] = True
                elif filename.endswith('.jar'):
                    score += 50
                    details['is_jar'] = True
                
                # Bonus for Spring Boot executable JARs (common pattern)
                if 'spring' in filename or 'boot' in filename:
                    score += 200
                    details['spring_boot'] = True
                
                # Bonus for files without version numbers (cleaner naming)
                if not any(char.isdigit() for char in filename):
                    score += 50
                    details['no_version'] = True
                
                # Bonus for standalone/executable naming patterns
                standalone_patterns = ['standalone', 'executable', 'runner', 'main', 'server']
                for pattern in standalone_patterns:
                    if pattern in filename:
                        score += 150
                        details['standalone_pattern'] = pattern
                        break
                
                # Penalty for files with common utility/tool names
                utility_keywords = ['util', 'helper', 'common', 'shared', 'tool', 'config',
                                   'wrapper', 'adapter', 'client', 'proxy']
                for keyword in utility_keywords:
                    if keyword in filename:
                        score -= 200
                        details['utility_keyword'] = keyword
                        break
                
                # Penalty for test/demo artifacts
                test_keywords = ['test', 'demo', 'sample', 'example', 'mock']
                for keyword in test_keywords:
                    if keyword in filename:
                        score -= 300
                        details['test_keyword'] = keyword
                        break
                
                # Penalty for deeply nested files
                if path_depth > 4:
                    score -= 100
                    details['deeply_nested'] = True
                
                # Penalty for files in target/build directories (build artifacts)
                if '/target/' in filepath_lower or '/build/' in filepath_lower:
                    score -= 50
                    details['build_dir'] = True
                
                # Bonus for files in standard Java EE deployment locations
                javaee_paths = ['/deployments/', '/standalone/deployments/', '/domain/deployments/']
                for jee_path in javaee_paths:
                    if jee_path in filepath_lower:
                        score += 250
                        details['javaee_deployment'] = True
                        break
                
                return (score, priority if priority > 0 else 5, filepath, details)
            
            # Get file sizes for better scoring (with container running temporarily)
            file_sizes = {}
            try:
                # Start a temporary container to check file sizes
                logger.info("Analyzing artifact file sizes...")
                temp_result = subprocess.run(
                    ['docker', 'run', '--rm', '--entrypoint', 'sh', image_url, '-c',
                     f"stat -c '%n %s' {' '.join(app_files)} 2>/dev/null"],
                    capture_output=True,
                    text=True,
                    encoding='utf-8',
                    errors='replace',
                    timeout=30
                )
                
                if temp_result.returncode == 0 and temp_result.stdout.strip():
                    for line in temp_result.stdout.strip().split('\n'):
                        parts = line.rsplit(' ', 1)
                        if len(parts) == 2:
                            try:
                                file_path, size = parts
                                file_sizes[file_path] = int(size)
                            except ValueError:
                                pass
            except (subprocess.TimeoutExpired, subprocess.CalledProcessError) as e:
                logger.warning(f"Could not retrieve file sizes, proceeding without size analysis: {e}")
            
            # Score all artifacts with file size information and metadata hints
            scored_artifacts = []
            for f in app_files:
                file_size = file_sizes.get(f, 0)
                score_tuple = score_artifact(f, app_name, file_size, artifact_hints)
                scored_artifacts.append(score_tuple)
            
            # Sort by score (descending), then by priority (ascending)
            scored_artifacts.sort(key=lambda x: (-x[0], x[1]))
            
            # Select the best artifact
            selected_artifact = scored_artifacts[0][2]
            selected_score = scored_artifacts[0][0]
            selected_priority = scored_artifacts[0][1]
            selected_details = scored_artifacts[0][3]
            
            logger.info(f"Selected artifact (score: {selected_score}, priority: {selected_priority}): {selected_artifact}")
            
            # Log detailed selection reasoning if multiple candidates
            if len(app_files) > 1:
                logger.info("Selection reasoning:")
                logger.info(f"  - Final Score: {selected_score}")
                logger.info(f"  - Priority Level: {selected_priority}")
                
                # Log key factors that influenced the decision
                key_factors = []
                if selected_details.get('metadata_match'):
                    key_factors.append(f"⭐ Docker metadata match: {selected_details.get('metadata_hint', 'N/A')}")
                if selected_details.get('eap_jboss_path'):
                    key_factors.append(f"Red Hat EAP/JBoss path: {selected_details['eap_jboss_path']}")
                if selected_details.get('eap_jboss_pattern'):
                    key_factors.append(f"Red Hat EAP/JBoss pattern: {selected_details['eap_jboss_pattern']}")
                if selected_details.get('exact_match'):
                    key_factors.append("Exact name match (app.jar/war)")
                if selected_details.get('app_name_match'):
                    key_factors.append(f"Matches app name '{app_name}'")
                if selected_details.get('preferred_path'):
                    key_factors.append(f"In preferred path: {selected_details['preferred_path']}")
                if selected_details.get('size_mb'):
                    key_factors.append(f"Size: {selected_details['size_mb']} MB ({selected_details.get('size_category', 'unknown')})")
                if selected_details.get('spring_boot'):
                    key_factors.append("Spring Boot application")
                if selected_details.get('javaee_deployment'):
                    key_factors.append("Java EE deployment location")
                if selected_details.get('is_war'):
                    key_factors.append("WAR file (web application)")
                
                if key_factors:
                    logger.info(f"  - Key Factors: {', '.join(key_factors)}")
                
                # Show comparison with next best option
                if len(scored_artifacts) > 1:
                    next_best = scored_artifacts[1]
                    logger.info(f"  - Next Best Option:")
                    logger.info(f"    * File: {next_best[2]}")
                    logger.info(f"    * Score: {next_best[0]} (difference: {selected_score - next_best[0]})")
                    if next_best[3].get('size_mb'):
                        logger.info(f"    * Size: {next_best[3]['size_mb']} MB")
                
                # Show all candidates with scores for full transparency
                if len(scored_artifacts) > 2:
                    logger.info(f"  - All Candidates Summary:")
                    for idx, (score, prio, path, details) in enumerate(scored_artifacts[:5], 1):
                        size_info = f" ({details.get('size_mb', 0)} MB)" if details.get('size_mb') else ""
                        logger.info(f"    {idx}. Score {score}: {os.path.basename(path)}{size_info}")
            
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
        
        success = False
        
        try:
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
            
            success = True
            return True
            
        finally:
            # Cleanup after processing (success or failure)
            if self.cleanup:
                logger.info(f"Performing cleanup for {app_name}...")
                
                # Clean up Docker image
                self.cleanup_docker_image(image_url)
                
                # Clean up artifacts only if processing was successful
                if success:
                    self.cleanup_artifacts(app_name)
                else:
                    logger.info(f"Keeping artifacts for failed application: {app_name}")
    
    def process_application_wrapper(self, app: Dict[str, str]) -> Tuple[str, bool]:
        """
        Wrapper for process_application to handle exceptions in thread pool
        
        Args:
            app: Dictionary with app_name and image_url
            
        Returns:
            Tuple of (app_name, success_status)
        """
        app_name = app['app_name']
        image_url = app['image_url']
        
        try:
            success = self.process_application(app_name, image_url)
            return (app_name, success)
        except Exception as e:
            logger.error(f"Error processing {app_name}: {e}")
            import traceback
            logger.error(traceback.format_exc())
            # Cleanup on exception
            if self.cleanup:
                logger.info(f"Cleaning up after error for {app_name}...")
                self.cleanup_docker_image(image_url)
            return (app_name, False)
    
    def process_all(self, csv_path: str) -> Dict[str, bool]:
        """
        Process all applications from CSV file using multi-threading
        
        Args:
            csv_path: Path to input CSV file
            
        Returns:
            Dictionary mapping app_name to success status
        """
        results = {}
        start_time = time.time()
        
        try:
            applications = self.read_input_csv(csv_path)
            
            if not applications:
                logger.warning("No applications found in CSV file")
                return results
            
            logger.info(f"\n{'='*60}")
            logger.info(f"Starting parallel processing with {self.max_workers} threads")
            logger.info(f"{'='*60}\n")
            
            # Process applications in parallel using ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                # Submit all tasks
                future_to_app = {
                    executor.submit(self.process_application_wrapper, app): app['app_name']
                    for app in applications
                }
                
                # Collect results as they complete
                for future in as_completed(future_to_app):
                    app_name = future_to_app[future]
                    try:
                        result_app_name, success = future.result()
                        results[result_app_name] = success
                        logger.info(f"Completed {len(results)}/{len(applications)} applications")
                    except Exception as e:
                        logger.error(f"Exception for {app_name}: {e}")
                        results[app_name] = False
            
            # Calculate elapsed time
            elapsed_time = time.time() - start_time
            
            # Summary
            logger.info(f"\n{'='*60}")
            logger.info("PROCESSING SUMMARY")
            logger.info(f"{'='*60}")
            successful = sum(1 for v in results.values() if v)
            failed = len(results) - successful
            logger.info(f"Total applications: {len(results)}")
            logger.info(f"Successful: {successful}")
            logger.info(f"Failed: {failed}")
            logger.info(f"Total time: {elapsed_time:.2f} seconds")
            logger.info(f"Average time per app: {elapsed_time/len(results):.2f} seconds")
            
            if self.cleanup:
                logger.info(f"Cleanup mode: ENABLED (artifacts removed after successful processing)")
            else:
                logger.info(f"Cleanup mode: DISABLED")
                logger.info(f"Artifacts directory: {os.path.abspath(self.output_dir)}")
            
            for app_name, success in results.items():
                status = "✓ SUCCESS" if success else "✗ FAILED"
                logger.info(f"  {app_name}: {status}")
            
        except Exception as e:
            logger.error(f"Error in process_all: {e}")
            import traceback
            logger.error(traceback.format_exc())
        finally:
            # Final cleanup of any remaining Docker images
            if self.cleanup and self.processed_images:
                logger.info("\nPerforming final cleanup of remaining Docker images...")
                self.cleanup_all_docker_images()
        
        return results


def parse_arguments():
    """Parse command line arguments"""
    parser = argparse.ArgumentParser(
        description='MTA (Migration Toolkit for Applications) Analyzer - Multi-threaded version',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
Examples:
  # Basic usage with 4 threads (default)
  python mta_analyzer_multithreaded.py https://mta.example.com input.csv
  
  # Use 8 threads for faster processing
  python mta_analyzer_multithreaded.py https://mta.example.com input.csv --threads 8
  
  # Disable cleanup to keep Docker images and artifacts
  python mta_analyzer_multithreaded.py https://mta.example.com input.csv --no-cleanup
  
  # Specify custom output directory with 6 threads
  python mta_analyzer_multithreaded.py https://mta.example.com input.csv --output-dir /path/to/artifacts --threads 6
  
  # Enable debug logging
  python mta_analyzer_multithreaded.py https://mta.example.com input.csv --debug
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
        '-t', '--threads',
        type=int,
        default=4,
        help='Maximum number of concurrent threads (default: 4)'
    )
    
    parser.add_argument(
        '--no-cleanup',
        action='store_true',
        help='Disable automatic cleanup of Docker images and artifacts after processing'
    )
    
    parser.add_argument(
        '-d', '--debug',
        action='store_true',
        help='Enable debug logging'
    )
    
    parser.add_argument(
        '-v', '--version',
        action='version',
        version='MTA Analyzer Multi-threaded 2.0.0'
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
    
    # Validate thread count
    if args.threads < 1:
        logger.error("Thread count must be at least 1")
        sys.exit(1)
    
    # Determine cleanup mode (default is True, disabled with --no-cleanup)
    cleanup_enabled = not args.no_cleanup
    
    logger.info("="*60)
    logger.info("MTA Analyzer Multi-threaded v2.0.0")
    logger.info("="*60)
    logger.info(f"MTA URL: {args.mta_url}")
    logger.info(f"Input CSV: {args.input_csv}")
    logger.info(f"Output Directory: {args.output_dir}")
    logger.info(f"Max Threads: {args.threads}")
    logger.info(f"Cleanup Mode: {'ENABLED' if cleanup_enabled else 'DISABLED'}")
    logger.info("="*60)
    
    analyzer = None
    try:
        analyzer = MTAAnalyzer(
            args.mta_url,
            output_dir=args.output_dir,
            cleanup=cleanup_enabled,
            max_workers=args.threads
        )
        results = analyzer.process_all(args.input_csv)
        
        # Exit with error code if any application failed
        if any(not success for success in results.values()):
            logger.error("\nSome applications failed to process")
            sys.exit(1)
        
        logger.info("\n" + "="*60)
        logger.info("MTA Analyzer completed successfully!")
        if cleanup_enabled:
            logger.info("All Docker images and successful artifacts have been cleaned up")
        logger.info("="*60)
        
    except KeyboardInterrupt:
        logger.warning("\n\nProcess interrupted by user")
        # Attempt cleanup on interrupt if enabled
        if cleanup_enabled and analyzer:
            logger.info("Performing cleanup before exit...")
            try:
                analyzer.cleanup_all_docker_images()
            except:
                pass
        sys.exit(130)
    except Exception as e:
        logger.error(f"\nFatal error: {e}")
        import traceback
        logger.error(traceback.format_exc())
        sys.exit(1)


if __name__ == '__main__':
    main()

# Made with Bob - Multi-threaded version