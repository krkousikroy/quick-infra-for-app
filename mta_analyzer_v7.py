#!/usr/bin/env python3
"""
MTA Multi-Language Analyzer V7
Final version with:
- Dynamic path detection for all languages
- MTA operations ONLY for Java binaries
- Detailed per-image CSV summary with all metadata
- Files stored in app subdirectories for manual GitLab push
"""

import os
import sys
import json
import subprocess
import csv
import logging
import argparse
import shutil
from typing import Dict, List, Optional, Tuple, Any
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
from datetime import datetime

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('mta_analyzer_v7.log', encoding='utf-8', errors='replace'),
        logging.StreamHandler(sys.stdout)
    ]
)

logger = logging.getLogger(__name__)


class ApplicationType:
    """Application type constants"""
    JAVA = "java"
    PYTHON = "python"
    GO = "go"
    JAVASCRIPT = "javascript"
    DOTNET = "dotnet"
    INFRASTRUCTURE = "infrastructure"
    DATABASE = "database"
    UNKNOWN = "unknown"


class MTAAnalyzerV7:
    """Enhanced MTA Analyzer - MTA operations only for Java"""
    
    def __init__(self, mta_url: str, output_dir: str = 'artifacts',
                 cleanup: bool = True, max_workers: int = 4,
                 summary_csv: str = 'mta_analysis_summary.csv'):
        """
        Initialize MTA Analyzer V7
        
        Args:
            mta_url: Base URL of MTA installation (only used for Java apps)
            output_dir: Directory to store extracted artifacts
            cleanup: Whether to cleanup Docker images after processing
            max_workers: Maximum number of concurrent threads
            summary_csv: Path to consolidated summary CSV file
        """
        self.mta_url = mta_url.rstrip('/') if mta_url else ''
        self.output_dir = output_dir
        self.cleanup = cleanup
        self.max_workers = max_workers
        self.summary_csv = summary_csv
        self.processed_images = []
        self.images_lock = Lock()
        self.csv_lock = Lock()
        os.makedirs(output_dir, exist_ok=True)
        
        # Initialize consolidated CSV with headers if it doesn't exist
        if not os.path.exists(self.summary_csv):
            self._initialize_summary_csv()
    
    def _initialize_summary_csv(self):
        """Initialize consolidated summary CSV with headers"""
        try:
            with open(self.summary_csv, 'w', newline='', encoding='utf-8', errors='replace') as csvfile:
                writer = csv.writer(csvfile)
                writer.writerow([
                    'Image URL',
                    'App Name',
                    'Detected Language/Type',
                    'Infrastructure Subtype',
                    'Number of Volumes',
                    'Volume Paths',
                    'Number of Exposed Ports',
                    'Exposed Ports',
                    'ENTRYPOINT',
                    'CMD',
                    'WORKDIR',
                    'USER',
                    'Shell',
                    'Image ID',
                    'Created Date',
                    'Architecture',
                    'OS',
                    'Size (bytes)',
                    'Layer Count',
                    'Dynamic Paths Detected',
                    'Sample Detected Files',
                    'Extracted To',
                    'Extraction Status',
                    'Notes',
                    'Processing Timestamp'
                ])
            logger.info(f"Initialized consolidated summary CSV: {self.summary_csv}")
        except Exception as e:
            logger.error(f"Failed to initialize summary CSV: {e}")
    
    def _append_to_summary_csv(self, app_name: str, image_url: str, app_type: str,
                                metadata: Dict[str, Any], extracted_path: Optional[str],
                                skip_reason: str = ''):
        """Append analysis results to consolidated CSV (thread-safe) with error handling"""
        max_retries = 3
        retry_count = 0
        
        while retry_count < max_retries:
            try:
                # Determine status and notes
                if extracted_path:
                    status = 'Success'
                    notes = ''
                elif skip_reason:
                    status = 'Skipped'
                    notes = skip_reason
                else:
                    status = 'Failed'
                    notes = ''
                
                # Sanitize strings to handle encoding issues
                def sanitize(value):
                    if isinstance(value, str):
                        return value.encode('utf-8', errors='replace').decode('utf-8', errors='replace')
                    return value
                
                with self.csv_lock:
                    with open(self.summary_csv, 'a', newline='', encoding='utf-8', errors='replace') as csvfile:
                        writer = csv.writer(csvfile)
                        writer.writerow([
                            sanitize(image_url),
                            sanitize(app_name),
                            sanitize(app_type),
                            sanitize(metadata.get('infrastructure_subtype', '')),
                            len(metadata.get('volumes', [])),
                            sanitize('; '.join(metadata.get('volumes', []))),
                            len(metadata.get('exposed_ports', [])),
                            sanitize('; '.join(metadata.get('exposed_ports', []))),
                            sanitize(' '.join(metadata.get('entrypoint', []))),
                            sanitize(' '.join(metadata.get('cmd', []))),
                            sanitize(metadata.get('workdir', '')),
                            sanitize(metadata.get('user', '')),
                            sanitize(' '.join(metadata.get('shell', []))),
                            sanitize(metadata.get('image_id', '')),
                            sanitize(metadata.get('created', '')),
                            sanitize(metadata.get('architecture', '')),
                            sanitize(metadata.get('os', '')),
                            metadata.get('size', 0),
                            metadata.get('layer_count', 0),
                            sanitize('; '.join(metadata.get('dynamic_paths', []))),
                            sanitize('; '.join(metadata.get('detected_files', [])[:5])),
                            sanitize(extracted_path or 'N/A'),
                            sanitize(status),
                            sanitize(notes),
                            datetime.now().isoformat()
                        ])
                logger.info(f"Appended {app_name} to consolidated summary CSV")
                return  # Success, exit retry loop
                
            except UnicodeEncodeError as e:
                logger.error(f"Encoding error appending to CSV (attempt {retry_count + 1}/{max_retries}): {e}")
                retry_count += 1
                if retry_count >= max_retries:
                    logger.error(f"Failed to append {app_name} after {max_retries} attempts")
            except PermissionError as e:
                logger.error(f"Permission denied writing to CSV: {e}")
                break  # Don't retry permission errors
            except Exception as e:
                logger.error(f"Unexpected error appending to CSV (attempt {retry_count + 1}/{max_retries}): {type(e).__name__}: {e}")
                retry_count += 1
                if retry_count >= max_retries:
                    logger.error(f"Failed to append {app_name} after {max_retries} attempts")
    
    def read_input_csv(self, csv_path: str) -> List[Dict[str, str]]:
        """Read applications from CSV file with comprehensive error handling"""
        applications = []
        
        try:
            with open(csv_path, 'r', encoding='utf-8', errors='replace') as csvfile:
                reader = csv.DictReader(csvfile)
                row_num = 0
                for row in reader:
                    row_num += 1
                    try:
                        if 'app_name' in row and 'image_url' in row:
                            app_name = row['app_name'].strip()
                            image_url = row['image_url'].strip()
                            
                            if app_name and image_url:
                                applications.append({
                                    'app_name': app_name,
                                    'image_url': image_url
                                })
                            else:
                                logger.warning(f"Row {row_num}: Empty app_name or image_url, skipping")
                        else:
                            logger.warning(f"Row {row_num}: Missing required columns (app_name, image_url)")
                    except Exception as e:
                        logger.error(f"Error processing row {row_num}: {e}, skipping")
                        continue
            
            logger.info(f"Read {len(applications)} valid applications from {csv_path}")
            return applications
        
        except FileNotFoundError:
            logger.error(f"CSV file not found: {csv_path}")
            return []
        except UnicodeDecodeError as e:
            logger.error(f"Encoding error reading CSV file: {e}")
            logger.info(f"Attempting to read with different encoding...")
            try:
                with open(csv_path, 'r', encoding='latin-1', errors='replace') as csvfile:
                    reader = csv.DictReader(csvfile)
                    for row in reader:
                        if 'app_name' in row and 'image_url' in row:
                            applications.append({
                                'app_name': row['app_name'].strip(),
                                'image_url': row['image_url'].strip()
                            })
                logger.info(f"Successfully read {len(applications)} applications with latin-1 encoding")
                return applications
            except Exception as e2:
                logger.error(f"Failed to read CSV with alternative encoding: {e2}")
                return []
        except PermissionError:
            logger.error(f"Permission denied reading CSV file: {csv_path}")
            return []
        except Exception as e:
            logger.error(f"Unexpected error reading CSV file: {type(e).__name__}: {e}")
            return []
    
    def pull_docker_image(self, image_url: str) -> bool:
        """Pull Docker image with comprehensive error handling"""
        logger.info(f"Pulling Docker image: {image_url}")
        
        try:
            result = subprocess.run(
                ['docker', 'pull', image_url],
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                timeout=600
            )
            
            if result.returncode == 0:
                logger.info(f"Successfully pulled image: {image_url}")
                with self.images_lock:
                    if image_url not in self.processed_images:
                        self.processed_images.append(image_url)
                return True
            else:
                logger.error(f"Failed to pull image: {result.stderr}")
                return False
        
        except subprocess.TimeoutExpired:
            logger.error(f"Timeout pulling image (600s exceeded): {image_url}")
            return False
        except FileNotFoundError:
            logger.error(f"Docker command not found. Please ensure Docker is installed and in PATH")
            return False
        except UnicodeDecodeError as e:
            logger.error(f"Encoding error while pulling image: {e}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error pulling image: {type(e).__name__}: {e}")
            return False
    
    def get_full_image_metadata(self, image_url: str) -> Dict[str, Any]:
        """
        Get comprehensive metadata from Docker image with error handling
        """
        metadata = {
            'entrypoint': [],
            'cmd': [],
            'env': {},
            'labels': {},
            'workdir': '',
            'exposed_ports': [],
            'volumes': [],
            'user': '',
            'shell': [],
            'image_id': '',
            'created': '',
            'architecture': '',
            'os': '',
            'size': 0,
            'layer_count': 0,
            'detected_files': [],
            'dynamic_paths': [],
            'infrastructure_subtype': ''
        }
        
        try:
            result = subprocess.run(
                ['docker', 'inspect', image_url],
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                timeout=10
            )
            
            if result.returncode == 0:
                try:
                    inspect_data = json.loads(result.stdout)
                except json.JSONDecodeError as e:
                    logger.error(f"Failed to parse docker inspect JSON: {e}")
                    return metadata
                
                if inspect_data and len(inspect_data) > 0:
                    config = inspect_data[0].get('Config', {})
                    
                    # Basic config
                    metadata['entrypoint'] = config.get('Entrypoint') or []
                    metadata['cmd'] = config.get('Cmd') or []
                    metadata['workdir'] = config.get('WorkingDir', '')
                    metadata['labels'] = config.get('Labels') or {}
                    metadata['user'] = config.get('User', '')
                    metadata['shell'] = config.get('Shell') or []
                    
                    # Extract ENV variables
                    env_list = config.get('Env') or []
                    for env_var in env_list:
                        if '=' in env_var:
                            key, value = env_var.split('=', 1)
                            metadata['env'][key] = value
                    
                    # Exposed ports
                    exposed_ports = config.get('ExposedPorts', {})
                    if exposed_ports:
                        metadata['exposed_ports'] = list(exposed_ports.keys())
                    
                    # Volumes
                    volumes = config.get('Volumes', {})
                    if volumes:
                        metadata['volumes'] = list(volumes.keys())
                    
                    # Image info
                    metadata['image_id'] = inspect_data[0].get('Id', '')[:12]
                    metadata['created'] = inspect_data[0].get('Created', '')
                    metadata['architecture'] = inspect_data[0].get('Architecture', '')
                    metadata['os'] = inspect_data[0].get('Os', '')
                    metadata['size'] = inspect_data[0].get('Size', 0)
                    
                    # Layers
                    root_fs = inspect_data[0].get('RootFS', {})
                    layers = root_fs.get('Layers', [])
                    metadata['layer_count'] = len(layers)
            
            return metadata
        
        except subprocess.TimeoutExpired:
            logger.error(f"Timeout inspecting image (10s exceeded): {image_url}")
            return metadata
        except FileNotFoundError:
            logger.error(f"Docker command not found during inspect")
            return metadata
        except UnicodeDecodeError as e:
            logger.error(f"Encoding error during inspect: {e}")
            return metadata
        except Exception as e:
            logger.error(f"Unexpected error getting image metadata: {type(e).__name__}: {e}")
            return metadata
    
    def detect_application_type(self, image_url: str, metadata: Dict[str, Any]) -> str:
        """
        Detect application type with enhanced infrastructure detection
        """
        logger.info(f"Detecting application type for: {image_url}")
        
        # Strategy 1: Check for infrastructure/database first
        app_type = self._detect_infrastructure_type(metadata)
        if app_type:
            logger.info(f"Detected infrastructure: {app_type} ({metadata.get('infrastructure_subtype', '')})")
            return app_type
        
        # Strategy 2: Analyze metadata for application types
        app_type = self._analyze_metadata_for_type(metadata)
        if app_type != ApplicationType.UNKNOWN:
            logger.info(f"Detected via metadata: {app_type}")
            return app_type
        
        # Strategy 3: Scan container filesystem
        app_type = self._scan_container_for_type(image_url, metadata)
        if app_type != ApplicationType.UNKNOWN:
            logger.info(f"Detected via filesystem: {app_type}")
            return app_type
        
        logger.info(f"Could not determine application type, defaulting to UNKNOWN")
        return ApplicationType.UNKNOWN
    
    def _detect_infrastructure_type(self, metadata: Dict[str, Any]) -> str:
        """Enhanced infrastructure/database detection"""
        
        all_commands = ' '.join(
            metadata.get('entrypoint', []) + 
            metadata.get('cmd', [])
        ).lower()
        
        env_str = ' '.join(
            f"{k}={v}" for k, v in metadata.get('env', {}).items()
        ).lower()
        
        labels_str = ' '.join(
            f"{k}={v}" for k, v in metadata.get('labels', {}).items()
        ).lower()
        
        combined = f"{all_commands} {env_str} {labels_str}"
        
        infrastructure_patterns = {
            'nginx': ['nginx', 'nginx.conf', '/etc/nginx', '/usr/sbin/nginx'],
            'apache': ['apache', 'httpd', 'apache2', '/etc/apache', '/usr/sbin/apache2', '/usr/sbin/httpd'],
            'redis': ['redis-server', 'redis.conf', 'redis-cli', '/usr/bin/redis-server'],
            'memcached': ['memcached', 'memcache'],
            'rabbitmq': ['rabbitmq', 'rabbitmq-server', 'rabbitmq-plugins'],
            'kafka': ['kafka', 'zookeeper', 'kafka-server', 'kafka-broker'],
            'elasticsearch': ['elasticsearch', 'elastic', '/usr/share/elasticsearch'],
            'haproxy': ['haproxy', 'haproxy.cfg'],
            'traefik': ['traefik'],
            'envoy': ['envoy'],
            'apex': ['apex', 'oracle apex', 'ords'],
        }
        
        database_patterns = {
            'postgres': ['postgres', 'postgresql', 'psql', '/usr/lib/postgresql'],
            'mysql': ['mysql', 'mysqld', 'mariadb', '/usr/sbin/mysqld'],
            'mongodb': ['mongo', 'mongod', '/usr/bin/mongod'],
            'cassandra': ['cassandra', '/usr/sbin/cassandra'],
            'couchdb': ['couchdb'],
            'influxdb': ['influxdb'],
            'neo4j': ['neo4j'],
            'oracle': ['oracle', 'sqlplus', 'oracle database'],
            'mssql': ['mssql', 'sqlserver', 'sql server'],
        }
        
        for infra_type, patterns in infrastructure_patterns.items():
            if any(pattern in combined for pattern in patterns):
                metadata['infrastructure_subtype'] = infra_type
                return ApplicationType.INFRASTRUCTURE
        
        for db_type, patterns in database_patterns.items():
            if any(pattern in combined for pattern in patterns):
                metadata['infrastructure_subtype'] = f"database-{db_type}"
                return ApplicationType.DATABASE
        
        return ""
    
    def _analyze_metadata_for_type(self, metadata: Dict[str, Any]) -> str:
        """Analyze metadata with enhanced path detection"""
        
        all_commands = ' '.join(
            metadata.get('entrypoint', []) +
            metadata.get('cmd', [])
        ).lower()
        
        env_str = ' '.join(
            f"{k}={v}" for k, v in metadata.get('env', {}).items()
        ).lower()
        
        workdir = metadata.get('workdir', '').lower()
        
        detection_patterns = {
            ApplicationType.JAVA: {
                'patterns': ['.jar', '.war', '.ear', 'java -jar', 'catalina', 'jboss', 'wildfly'],
                'paths': ['/deployments', '/opt/jboss', '/opt/wildfly', '/app', '/opt/app']
            },
            ApplicationType.PYTHON: {
                'patterns': ['python', 'python3', 'pip', 'django', 'flask', 'uvicorn', 'gunicorn'],
                'paths': ['/app', '/usr/src/app', '/opt/app', '/code']
            },
            ApplicationType.GO: {
                'patterns': ['go run', 'go build', 'golang'],
                'paths': ['/app', '/go/src', '/opt/app']
            },
            ApplicationType.JAVASCRIPT: {
                'patterns': ['node', 'npm', 'yarn', 'npm start', 'node index'],
                'paths': ['/app', '/code', '/usr/src/app', '/opt/app']
            },
            ApplicationType.DOTNET: {
                'patterns': ['dotnet', 'dotnet run', 'dotnet exec', '.dll', 'aspnetcore', 'entrypoint.sh'],
                'paths': ['/app', '/data/coreapp/publish', '/opt/app', '/src']
            }
        }
        
        scores = {}
        for app_type, config in detection_patterns.items():
            score = 0
            
            for pattern in config['patterns']:
                if pattern in all_commands:
                    score += 10
                elif pattern in env_str:
                    score += 5
            
            for path in config['paths']:
                if path in workdir or path in all_commands:
                    score += 5
                    if path not in metadata['dynamic_paths']:
                        metadata['dynamic_paths'].append(path)
            
            if score > 0:
                scores[app_type] = score
        
        if scores:
            best_match = max(scores.items(), key=lambda x: x[1])
            if best_match[1] >= 5:
                return best_match[0]
        
        return ApplicationType.UNKNOWN
    
    def _scan_container_for_type(self, image_url: str, metadata: Dict[str, Any]) -> str:
        """Scan container filesystem with enhanced path detection and file counting"""
        
        detection_config = {
            ApplicationType.JAVA: {
                'patterns': ['*.war', '*.ear', '*.jar'],
                'paths': ['/deployments', '/opt/jboss', '/opt/wildfly', '/app', '/opt/app'],
                'min_score': 5
            },
            ApplicationType.PYTHON: {
                'patterns': ['*.py', 'requirements.txt', 'setup.py'],
                'paths': ['/app', '/usr/src/app', '/opt/app', '/code', '/src'],
                'min_score': 10  # Need at least 2 Python files
            },
            ApplicationType.GO: {
                'patterns': ['*.go', 'go.mod', 'go.sum'],
                'paths': ['/app', '/go/src', '/opt/app', '/src'],
                'min_score': 5
            },
            ApplicationType.JAVASCRIPT: {
                'patterns': ['*.js', '*.ts', 'package.json', 'tsconfig.json'],
                'paths': ['/app', '/code', '/usr/src/app', '/opt/app', '/src'],
                'min_score': 10  # Need at least 2 JS/TS files
            },
            ApplicationType.DOTNET: {
                'patterns': ['*.cs', '*.csproj', '*.sln'],
                'paths': ['/app', '/data/coreapp/publish', '/opt/app', '/src'],
                'min_score': 5
            }
        }
        
        scores = {}
        
        for app_type, config in detection_config.items():
            score = 0
            found_paths = []
            
            for search_path in config['paths'][:3]:  # Check top 3 paths
                for pattern in config['patterns']:
                    try:
                        # Count files matching pattern
                        cmd = f"find {search_path} -maxdepth 4 -type f -name '{pattern}' 2>/dev/null | wc -l"
                        
                        result = subprocess.run(
                            ['docker', 'run', '--rm', '--entrypoint', 'sh', image_url, '-c', cmd],
                            capture_output=True,
                            text=True,
                            encoding='utf-8',
                            errors='replace',
                            timeout=15
                        )
                        
                        if result.returncode == 0 and result.stdout.strip():
                            try:
                                count = int(result.stdout.strip())
                                if count > 0:
                                    score += count * 5
                                    
                                    # Get sample file paths
                                    cmd_list = f"find {search_path} -maxdepth 4 -type f -name '{pattern}' 2>/dev/null | head -3"
                                    result_list = subprocess.run(
                                        ['docker', 'run', '--rm', '--entrypoint', 'sh', image_url, '-c', cmd_list],
                                        capture_output=True,
                                        text=True,
                                        encoding='utf-8',
                                        errors='replace',
                                        timeout=10
                                    )
                                    
                                    if result_list.returncode == 0 and result_list.stdout.strip():
                                        files = result_list.stdout.strip().split('\n')
                                        found_paths.extend(files)
                                        
                                        for file_path in files:
                                            dir_path = os.path.dirname(file_path)
                                            if dir_path and dir_path not in metadata['dynamic_paths']:
                                                metadata['dynamic_paths'].append(dir_path)
                            except ValueError as e:
                                logger.debug(f"Could not parse file count for {pattern}: {e}")
                                continue
                    
                    except subprocess.TimeoutExpired:
                        logger.debug(f"Timeout scanning {app_type} pattern {pattern} in {search_path}")
                        continue
                    except UnicodeDecodeError as e:
                        logger.debug(f"Encoding error scanning {app_type} pattern {pattern}: {e}")
                        continue
                    except Exception as e:
                        logger.debug(f"Scan error for {app_type} pattern {pattern}: {type(e).__name__}: {e}")
                        continue
            
            if score >= config['min_score']:
                scores[app_type] = score
                metadata['detected_files'].extend(found_paths[:5])
                logger.debug(f"{app_type}: score={score}, files found")
        
        if scores:
            best_match = max(scores.items(), key=lambda x: x[1])
            logger.info(f"Best match: {best_match[0]} with score {best_match[1]}")
            return best_match[0]
        
        return ApplicationType.UNKNOWN
    
    def extract_files_to_subdirectory(self, image_url: str, app_name: str, 
                                     app_type: str, metadata: Dict[str, Any]) -> Optional[str]:
        """Extract files to app-specific subdirectory"""
        
        logger.info(f"Extracting files for {app_type} application: {app_name}")
        
        app_dir = os.path.join(self.output_dir, app_name)
        os.makedirs(app_dir, exist_ok=True)
        
        if app_type == ApplicationType.JAVA:
            return self._extract_java_artifact(image_url, app_dir, metadata)
        elif app_type in [ApplicationType.INFRASTRUCTURE, ApplicationType.DATABASE]:
            return self._extract_infrastructure_files(image_url, app_dir, app_type, metadata)
        else:
            return self._extract_source_code(image_url, app_dir, app_type, metadata)
    
    def _extract_java_artifact(self, image_url: str, app_dir: str, 
                               metadata: Dict[str, Any]) -> Optional[str]:
        """Extract JAR/WAR artifact for Java applications with enhanced detection"""
        
        container_id = None
        try:
            result = subprocess.run(
                ['docker', 'create', image_url],
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                timeout=30
            )
            
            if result.returncode != 0:
                logger.error(f"Failed to create container: {result.stderr}")
                return None
            
            container_id = result.stdout.strip()
            if not container_id:
                logger.error(f"Empty container ID returned")
                return None
            
            # Enhanced search paths including common Java deployment locations
            search_paths = (
                metadata.get('dynamic_paths', []) + 
                ['/deployments', '/opt/jboss/wildfly/standalone/deployments',
                 '/usr/local/tomcat/webapps', '/usr/local/jetty/webapps',
                 '/var/lib/jetty/webapps', '/opt/app', '/app', '/usr/src/app']
            )
            
            found_artifacts = []
            search_log = []
            
            for search_path in search_paths[:8]:  # Check top 8 paths
                try:
                    # Find JAR/WAR files larger than 1KB (filter out tiny files)
                    cmd = f"find {search_path} -maxdepth 5 -type f \\( -name '*.jar' -o -name '*.war' -o -name '*.ear' \\) -size +1k 2>/dev/null"
                    
                    result = subprocess.run(
                        ['docker', 'run', '--rm', '--entrypoint', 'sh', image_url, '-c', cmd],
                        capture_output=True,
                        text=True,
                        encoding='utf-8',
                        errors='replace',
                        timeout=30
                    )
                    
                    if result.returncode == 0 and result.stdout.strip():
                        files = result.stdout.strip().split('\n')
                        for f in files:
                            f = f.strip()
                            if f and f not in found_artifacts:
                                found_artifacts.append(f)
                                search_log.append(f"Found in {search_path}")
                        
                        if found_artifacts:
                            break
                
                except subprocess.TimeoutExpired:
                    logger.warning(f"Timeout searching for artifacts in {search_path}")
                    continue
                except Exception as e:
                    logger.debug(f"Error searching {search_path}: {type(e).__name__}: {e}")
                    continue
            
            if not found_artifacts:
                logger.warning(f"No JAR/WAR/EAR files found in standard paths")
                logger.info("Note: Image may be a base Java runtime without deployed applications")
                return None
            
            logger.info(f"Found {len(found_artifacts)} Java artifact(s)")
            
            # Priority 1: WAR files (highest priority for web applications)
            war_files = [f for f in found_artifacts if f.endswith('.war')]
            
            # Priority 2: EAR files (enterprise applications)
            ear_files = [f for f in found_artifacts if f.endswith('.ear')]
            
            # Priority 3: Application JARs (exclude library JARs)
            app_jars = [
                f for f in found_artifacts
                if f.endswith('.jar') and not any(lib in f.lower() for lib in [
                    '/web-inf/lib/', '/lib/', '/libs/', '/library/',
                    'maven-', 'gradle-', 'spring-boot-loader', 'tomcat-embed'
                ])
            ]
            
            # Select best artifact based on priority
            if war_files:
                selected_artifact = war_files[0]
                logger.info(f"Selected WAR file (priority 1): {selected_artifact}")
            elif ear_files:
                selected_artifact = ear_files[0]
                logger.info(f"Selected EAR file (priority 2): {selected_artifact}")
            elif app_jars:
                selected_artifact = app_jars[0]
                logger.info(f"Selected application JAR (priority 3): {selected_artifact}")
            else:
                # No packaged artifacts found - likely exploded WAR deployment
                logger.warning(f"No WAR/EAR/JAR application artifacts found")
                logger.info(f"Note: Image may use exploded WAR deployment (unpacked structure)")
                logger.info(f"Skipping extraction - exploded deployments require manual source code extraction")
                return None
            
            artifact_name = os.path.basename(selected_artifact)
            local_path = os.path.join(app_dir, artifact_name)
            
            result = subprocess.run(
                ['docker', 'cp', f"{container_id}:{selected_artifact}", local_path],
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                timeout=120
            )
            
            if result.returncode == 0:
                if os.path.exists(local_path):
                    file_size = os.path.getsize(local_path)
                    logger.info(f"Extracted artifact: {artifact_name} ({file_size / 1024:.1f} KB)")
                    return local_path
                else:
                    logger.error(f"Artifact file not found after copy: {local_path}")
                    return None
            else:
                logger.error(f"Failed to copy artifact from container: {result.stderr}")
                return None
        
        except subprocess.TimeoutExpired:
            logger.error(f"Timeout extracting Java artifact (120s exceeded)")
            return None
        except OSError as e:
            logger.error(f"File system error extracting artifact: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error extracting Java artifact: {type(e).__name__}: {e}")
            return None
            
        finally:
            if container_id:
                subprocess.run(
                    ['docker', 'rm', '-f', container_id],
                    capture_output=True
                )
    
    def _extract_source_code(self, image_url: str, app_dir: str, 
                            app_type: str, metadata: Dict[str, Any]) -> Optional[str]:
        """Extract source code to local subdirectory with smart filtering"""
        
        search_paths = self._get_prioritized_paths(app_type, metadata)
        
        container_id = None
        try:
            result = subprocess.run(
                ['docker', 'create', image_url],
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                timeout=30
            )
            
            if result.returncode != 0:
                logger.error(f"Failed to create container for source extraction: {result.stderr}")
                return None
            
            container_id = result.stdout.strip()
            if not container_id:
                logger.error(f"Empty container ID returned for source extraction")
                return None
            
            extracted = False
            # Try non-root paths only (skip root directory to avoid full filesystem copy)
            non_root_paths = [p for p in search_paths if p != '/']
            
            if not non_root_paths:
                logger.warning(f"No specific paths to extract from, skipping full filesystem copy")
                logger.info(f"Note: Image appears to be a base/runtime image without application source code")
                return None
            
            for search_path in non_root_paths:
                try:
                    logger.info(f"Attempting to extract from: {search_path}")
                    
                    dest_path = os.path.join(app_dir, 'source')
                    result = subprocess.run(
                        ['docker', 'cp', f"{container_id}:{search_path}", dest_path],
                        capture_output=True,
                        text=True,
                        encoding='utf-8',
                        errors='replace',
                        timeout=120
                    )
                    
                    if result.returncode == 0:
                        # Clean up extracted files
                        try:
                            self._cleanup_extracted_files(dest_path, app_type, search_path)
                        except Exception as cleanup_error:
                            logger.warning(f"Error during cleanup: {cleanup_error}, continuing anyway")
                        
                        if self._verify_source_code(dest_path, app_type):
                            logger.info(f"Successfully extracted source code from {search_path}")
                            extracted = True
                            break
                        else:
                            logger.warning(f"Path {search_path} exists but contains insufficient source code")
                            try:
                                if os.path.exists(dest_path):
                                    shutil.rmtree(dest_path)
                            except Exception as rm_error:
                                logger.warning(f"Could not remove invalid extraction: {rm_error}")
                    else:
                        logger.debug(f"Could not extract from {search_path}: {result.stderr}")
                
                except subprocess.TimeoutExpired:
                    logger.warning(f"Timeout extracting from {search_path} (120s exceeded)")
                    continue
                except Exception as e:
                    logger.warning(f"Error extracting from {search_path}: {type(e).__name__}: {e}")
                    continue
            
            if not extracted:
                logger.warning(f"No valid source code found in any checked paths")
                logger.info(f"Note: Skipping extraction to avoid copying entire filesystem. Image may be a base/runtime image.")
                return None
            
            return dest_path
        
        except subprocess.TimeoutExpired:
            logger.error(f"Timeout during source code extraction")
            return None
        except OSError as e:
            logger.error(f"File system error during source extraction: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error extracting source code: {type(e).__name__}: {e}")
            return None
            
        finally:
            if container_id:
                try:
                    subprocess.run(
                        ['docker', 'rm', '-f', container_id],
                        capture_output=True,
                        timeout=30
                    )
                except Exception as e:
                    logger.warning(f"Could not remove container {container_id}: {e}")
    
    def _cleanup_extracted_files(self, path: str, app_type: str, search_path: str = ''):
        """Remove unnecessary files from extracted source code with error handling"""
        
        if not os.path.exists(path):
            logger.debug(f"Cleanup path does not exist: {path}")
            return
        
        if not os.path.isdir(path):
            logger.debug(f"Cleanup path is not a directory: {path}")
            return
        
        # Check if this looks like a full filesystem copy (has system dirs at root)
        is_full_fs = False
        try:
            root_items = os.listdir(path)
            system_indicators = ['bin', 'etc', 'usr', 'lib', 'var', 'proc', 'sys']
            system_count = sum(1 for item in root_items if item in system_indicators)
            if system_count >= 3:
                is_full_fs = True
                logger.warning(f"Detected full filesystem copy in {path}, removing system directories")
        except:
            pass
        
        # Directories to remove (only at root level if full FS, otherwise just build artifacts)
        system_dirs = ['proc', 'sys', 'dev', 'run', 'boot', 'mnt', 'media', 'root', 'home']
        build_dirs = ['node_modules', '__pycache__', '.git', '.svn', '.hg',
                     'venv', 'env', '.venv', 'virtualenv',
                     'build', 'dist', 'target', 'bin', 'obj',
                     '.pytest_cache', '.tox', '.coverage',
                     'tmp', 'temp', 'cache', 'logs']
        
        # File patterns to remove
        remove_patterns = [
            '*.pyc', '*.pyo', '*.so', '*.dylib', '*.dll',
            '*.log', '*.tmp', '*.cache', '*.pid',
            '.DS_Store', 'Thumbs.db', '*.swp', '*.swo'
        ]
        
        try:
            # If full filesystem, remove system directories at root level
            if is_full_fs:
                system_and_common = system_dirs + ['etc', 'usr', 'lib', 'lib64', 'var', 'sbin', 'opt']
                for dir_name in system_and_common:
                    dir_path = os.path.join(path, dir_name)
                    if os.path.exists(dir_path) and os.path.isdir(dir_path):
                        try:
                            shutil.rmtree(dir_path)
                            logger.debug(f"Removed system directory: {dir_name}")
                        except PermissionError as e:
                            logger.debug(f"Permission denied removing {dir_name}: {e}")
                        except OSError as e:
                            logger.debug(f"OS error removing {dir_name}: {e}")
                        except Exception as e:
                            logger.debug(f"Error removing {dir_name}: {type(e).__name__}: {e}")
                
                # Also remove common non-source files at root
                root_files_to_remove = ['.dockerenv', 'docker-entrypoint.sh']
                for file_name in root_files_to_remove:
                    file_path = os.path.join(path, file_name)
                    if os.path.exists(file_path) and os.path.isfile(file_path):
                        try:
                            os.remove(file_path)
                            logger.debug(f"Removed root file: {file_name}")
                        except PermissionError as e:
                            logger.debug(f"Permission denied removing {file_name}: {e}")
                        except OSError as e:
                            logger.debug(f"OS error removing {file_name}: {e}")
                        except Exception as e:
                            logger.debug(f"Error removing {file_name}: {type(e).__name__}: {e}")
            
            # Always remove build artifacts and dependencies
            for root, dirs, files in os.walk(path, topdown=False):
                for dir_name in dirs:
                    if dir_name in build_dirs:
                        dir_path = os.path.join(root, dir_name)
                        try:
                            shutil.rmtree(dir_path)
                            logger.debug(f"Removed build directory: {dir_name}")
                        except PermissionError:
                            logger.debug(f"Permission denied removing {dir_name}")
                        except OSError as e:
                            logger.debug(f"OS error removing {dir_name}: {e}")
                        except Exception as e:
                            logger.debug(f"Error removing {dir_name}: {type(e).__name__}")
            
            # Remove unwanted files
            import glob
            for pattern in remove_patterns:
                try:
                    for file_path in glob.glob(os.path.join(path, '**', pattern), recursive=True):
                        try:
                            os.remove(file_path)
                            logger.debug(f"Removed file: {os.path.basename(file_path)}")
                        except PermissionError:
                            logger.debug(f"Permission denied removing file")
                        except OSError as e:
                            logger.debug(f"OS error removing file: {e}")
                        except Exception as e:
                            logger.debug(f"Error removing file: {type(e).__name__}")
                except Exception as e:
                    logger.debug(f"Error processing pattern {pattern}: {e}")
            
            logger.info(f"Cleaned up extracted files in {path}")
        
        except PermissionError as e:
            logger.warning(f"Permission error during cleanup: {e}")
        except OSError as e:
            logger.warning(f"OS error during cleanup: {e}")
        except Exception as e:
            logger.warning(f"Unexpected error during cleanup: {type(e).__name__}: {e}")
    
    def _extract_infrastructure_files(self, image_url: str, app_dir: str,
                                     app_type: str, metadata: Dict[str, Any]) -> Optional[str]:
        """Extract configuration files for infrastructure components"""
        
        infra_subtype = metadata.get('infrastructure_subtype', '')
        
        config_paths = {
            'nginx': ['/etc/nginx', '/usr/share/nginx'],
            'apache': ['/etc/apache2', '/etc/httpd', '/usr/local/apache2'],
            'redis': ['/etc/redis', '/usr/local/etc/redis'],
            'postgres': ['/etc/postgresql', '/var/lib/postgresql'],
            'mysql': ['/etc/mysql', '/var/lib/mysql'],
            'mongodb': ['/etc/mongod.conf', '/data/db'],
        }
        
        search_paths = []
        for key, paths in config_paths.items():
            if key in infra_subtype:
                search_paths.extend(paths)
        
        if not search_paths:
            search_paths = ['/etc', '/config']
        
        container_id = None
        try:
            result = subprocess.run(
                ['docker', 'create', image_url],
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                timeout=30
            )
            
            if result.returncode != 0:
                logger.error(f"Failed to create container for infrastructure extraction: {result.stderr}")
                return None
            
            container_id = result.stdout.strip()
            if not container_id:
                logger.error(f"Empty container ID for infrastructure extraction")
                return None
            
            for search_path in search_paths[:3]:
                try:
                    dest_path = os.path.join(app_dir, 'config')
                    result = subprocess.run(
                        ['docker', 'cp', f"{container_id}:{search_path}", dest_path],
                        capture_output=True,
                        text=True,
                        encoding='utf-8',
                        errors='replace',
                        timeout=60
                    )
                    
                    if result.returncode == 0:
                        logger.info(f"Extracted config from {search_path}")
                        return dest_path
                    else:
                        logger.debug(f"Could not extract from {search_path}")
                
                except subprocess.TimeoutExpired:
                    logger.warning(f"Timeout extracting config from {search_path}")
                    continue
                except Exception as e:
                    logger.warning(f"Error extracting from {search_path}: {type(e).__name__}: {e}")
                    continue
            
            logger.warning(f"No infrastructure config files found in any search path")
            return None
        
        except subprocess.TimeoutExpired:
            logger.error(f"Timeout creating container for infrastructure extraction")
            return None
        except Exception as e:
            logger.error(f"Unexpected error extracting infrastructure files: {type(e).__name__}: {e}")
            return None
            
        finally:
            if container_id:
                try:
                    subprocess.run(
                        ['docker', 'rm', '-f', container_id],
                        capture_output=True,
                        timeout=30
                    )
                except Exception as e:
                    logger.warning(f"Could not remove container {container_id}: {e}")
    
    def _get_prioritized_paths(self, app_type: str, metadata: Dict[str, Any]) -> List[str]:
        """Get prioritized search paths with smart ordering"""
        
        paths = []
        
        # Priority 1: Dynamic paths detected from metadata
        dynamic_paths = metadata.get('dynamic_paths', [])
        paths.extend(dynamic_paths)
        
        # Priority 2: WORKDIR from Docker metadata
        workdir = metadata.get('workdir', '').strip()
        if workdir and workdir not in paths and workdir != '/':
            paths.append(workdir)
        
        # Priority 3: Conventional paths by language
        conventional_paths = {
            ApplicationType.PYTHON: ['/app', '/usr/src/app', '/opt/app', '/code', '/src'],
            ApplicationType.GO: ['/app', '/go/src', '/opt/app', '/src'],
            ApplicationType.JAVASCRIPT: ['/app', '/code', '/usr/src/app', '/opt/app', '/src'],
            ApplicationType.DOTNET: ['/data/coreapp/publish', '/app', '/opt/app', '/src']
        }
        
        type_paths = conventional_paths.get(app_type, ['/app', '/opt/app', '/src'])
        for path in type_paths:
            if path not in paths:
                paths.append(path)
        
        # Priority 4: Common source directories (if not already added)
        common_src_paths = ['/source', '/sources', '/workspace']
        for path in common_src_paths:
            if path not in paths:
                paths.append(path)
        
        return paths
    
    def _verify_source_code(self, path: str, app_type: str) -> bool:
        """Verify that extracted path contains valid source code with minimum threshold"""
        
        if not os.path.exists(path):
            return False
        
        verification_config = {
            ApplicationType.PYTHON: {
                'patterns': ['*.py'],
                'min_files': 2,  # At least 2 Python files
                'exclude_patterns': ['__pycache__']
            },
            ApplicationType.GO: {
                'patterns': ['*.go', 'go.mod'],
                'min_files': 1,
                'exclude_patterns': []
            },
            ApplicationType.JAVASCRIPT: {
                'patterns': ['*.js', '*.ts', 'package.json'],
                'min_files': 2,  # At least 2 JS/TS files or 1 + package.json
                'exclude_patterns': ['node_modules']
            },
            ApplicationType.DOTNET: {
                'patterns': ['*.cs', '*.csproj'],
                'min_files': 1,
                'exclude_patterns': ['bin', 'obj']
            }
        }
        
        config = verification_config.get(app_type, {'patterns': [], 'min_files': 1, 'exclude_patterns': []})
        patterns = config['patterns']
        min_files = config['min_files']
        
        import glob
        total_matches = 0
        
        for pattern in patterns:
            matches = glob.glob(os.path.join(path, '**', pattern), recursive=True)
            # Filter out excluded paths
            filtered_matches = [
                m for m in matches 
                if not any(excl in m for excl in config['exclude_patterns'])
            ]
            total_matches += len(filtered_matches)
            
            if total_matches >= min_files:
                logger.info(f"Verified source code: found {total_matches} {app_type} files")
                return True
        
        logger.warning(f"Insufficient source code: found only {total_matches} files (need {min_files})")
        return False
    

    
    def curl_request(self, method: str, url: str, data: Optional[str] = None,
                    file_path: Optional[str] = None) -> Tuple[int, str]:
        """Make HTTP request using curl (for Java MTA operations) with error handling"""
        
        cmd = ['curl', '-k', '-X', method, '-w', '\\n%{http_code}']
        
        if data:
            cmd.extend(['-H', 'Content-Type: application/json', '-d', data])
        
        if file_path:
            if not os.path.exists(file_path):
                logger.error(f"File not found for upload: {file_path}")
                return 0, "File not found"
            cmd.extend(['-F', f'file=@{file_path}'])
        
        cmd.append(url)
        
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                timeout=300
            )
            
            output = result.stdout
            lines = output.strip().split('\n')
            response_body = '\n'.join(lines[:-1]) if len(lines) > 1 else ''
            
            try:
                status_code = int(lines[-1]) if lines and lines[-1].isdigit() else 0
            except (ValueError, IndexError):
                logger.warning(f"Could not parse HTTP status code from curl output")
                status_code = 0
            
            return status_code, response_body
        
        except subprocess.TimeoutExpired:
            logger.error(f"Curl request timeout (300s exceeded) for {method} {url}")
            return 0, "Request timeout"
        except FileNotFoundError:
            logger.error(f"curl command not found. Please ensure curl is installed")
            return 0, "curl not found"
        except UnicodeDecodeError as e:
            logger.error(f"Encoding error in curl response: {e}")
            return 0, f"Encoding error: {e}"
        except Exception as e:
            logger.error(f"Unexpected curl request error: {type(e).__name__}: {e}")
            return 0, str(e)
    
    def process_java_with_mta(self, app_name: str, artifact_path: str) -> bool:
        """Process Java application with MTA (create app, task, upload, submit)"""
        
        logger.info(f"Processing Java application with MTA: {app_name}")
        
        try:
            # Create MTA application
            app_id = self._create_mta_application(app_name)
            if not app_id:
                logger.error(f"Failed to create MTA application")
                return False
            
            # Create binary analysis task
            artifact_name = os.path.basename(artifact_path)
            task_id = self._create_mta_task_binary(app_id, artifact_name)
            if not task_id:
                logger.error(f"Failed to create MTA task")
                return False
            
            # Upload artifact
            if not self._upload_artifact(task_id, artifact_path):
                logger.error(f"Failed to upload artifact")
                return False
            
            # Submit task
            if not self._submit_task(task_id):
                logger.error(f"Failed to submit task")
                return False
            
            logger.info(f"Successfully processed Java app in MTA")
            logger.info(f"  Application ID: {app_id}")
            logger.info(f"  Task ID: {task_id}")
            
            return True
            
        except Exception as e:
            logger.error(f"Error processing Java with MTA: {e}")
            return False
    
    def _create_mta_application(self, app_name: str) -> Optional[int]:
        """Create application in MTA"""
        
        logger.info(f"Creating MTA application: {app_name}")
        
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
                logger.error(f"Failed to parse response")
                return None
        else:
            logger.error(f"Failed to create application. Status: {status_code}")
            return None
    
    def _create_mta_task_binary(self, app_id: int, artifact_name: str) -> Optional[int]:
        """Create binary analysis task in MTA"""
        
        logger.info(f"Creating binary analysis task for app ID: {app_id}")
        
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
                task_response = json.loads(response)
                task_id = task_response.get('id')
                logger.info(f"Created task with ID: {task_id}")
                return task_id
            except json.JSONDecodeError:
                logger.error(f"Failed to parse response")
                return None
        else:
            logger.error(f"Failed to create task. Status: {status_code}")
            return None
    
    def _upload_artifact(self, task_id: int, artifact_path: str) -> bool:
        """Upload binary artifact to MTA task"""
        
        artifact_name = os.path.basename(artifact_path)
        logger.info(f"Uploading artifact: {artifact_name}")
        
        url = f"{self.mta_url}/hub/tasks/{task_id}/bucket/binary/{artifact_name}"
        
        file_size = os.path.getsize(artifact_path)
        file_size_mb = file_size / (1024 * 1024)
        logger.info(f"Artifact size: {file_size_mb:.2f} MB")
        
        status_code, response = self.curl_request('PUT', url, file_path=artifact_path)
        
        if status_code in [200, 201, 204]:
            logger.info(f"Successfully uploaded artifact")
            return True
        else:
            logger.error(f"Failed to upload artifact. Status: {status_code}")
            return False
    
    def _submit_task(self, task_id: int) -> bool:
        """Submit MTA task for execution"""
        
        logger.info(f"Submitting task: {task_id}")
        
        url = f"{self.mta_url}/hub/tasks/{task_id}/submit"
        data = json.dumps({"state": "Ready"})
        
        status_code, response = self.curl_request('PUT', url, data=data)
        
        if status_code in [200, 201, 204]:
            logger.info(f"Successfully submitted task")
            return True
        else:
            logger.error(f"Failed to submit task. Status: {status_code}")
            return False
    
    def process_application(self, app_name: str, image_url: str) -> bool:
        """Process application through complete workflow with error recovery"""
        
        logger.info(f"\n{'='*60}")
        logger.info(f"Processing application: {app_name}")
        logger.info(f"Image: {image_url}")
        logger.info(f"{'='*60}\n")
        
        metadata = {}
        app_type = ApplicationType.UNKNOWN
        extracted_path = None
        skip_reason = ''
        
        try:
            # Step 1: Pull Docker image
            if not self.pull_docker_image(image_url):
                logger.error(f"Failed to pull image for {app_name}")
                skip_reason = 'Failed to pull Docker image'
                self._append_to_summary_csv(app_name, image_url, app_type, metadata, None, skip_reason)
                return False
            
            # Step 2: Get full metadata
            try:
                metadata = self.get_full_image_metadata(image_url)
            except Exception as e:
                logger.error(f"Failed to get metadata for {app_name}: {e}")
                skip_reason = f'Failed to get image metadata: {type(e).__name__}'
                self._append_to_summary_csv(app_name, image_url, app_type, metadata, None, skip_reason)
                return False
            
            # Step 3: Detect application type
            try:
                app_type = self.detect_application_type(image_url, metadata)
                logger.info(f"Application type: {app_type}")
            except Exception as e:
                logger.error(f"Failed to detect application type for {app_name}: {e}")
                app_type = ApplicationType.UNKNOWN
                logger.info(f"Defaulting to UNKNOWN type, continuing with extraction")
            
            # Step 4: Extract files to subdirectory
            try:
                extracted_path = self.extract_files_to_subdirectory(image_url, app_name, app_type, metadata)
            except Exception as e:
                logger.error(f"Failed to extract files for {app_name}: {e}")
                skip_reason = f'Extraction failed: {type(e).__name__}'
                self._append_to_summary_csv(app_name, image_url, app_type, metadata, None, skip_reason)
                return False
            
            # Step 5: Determine skip reason if extraction failed
            if not extracted_path and app_type not in [ApplicationType.INFRASTRUCTURE, ApplicationType.DATABASE]:
                skip_reason = 'Base/runtime image without application source code - skipped to avoid full filesystem copy'
            
            # Step 6: Append to consolidated summary CSV
            self._append_to_summary_csv(app_name, image_url, app_type, metadata, extracted_path, skip_reason)
            
            # Step 7: For Java apps, process with MTA
            mta_success = False
            if app_type == ApplicationType.JAVA and extracted_path:
                logger.info(f"\n--- Processing Java application with MTA ---")
                try:
                    mta_success = self.process_java_with_mta(app_name, extracted_path)
                except Exception as e:
                    logger.error(f"MTA processing failed for {app_name}: {e}")
                    mta_success = False
            else:
                logger.info(f"\n--- Non-Java application: Files stored for manual GitLab push ---")
                logger.info(f"  Type: {app_type}")
                logger.info(f"  Files location: {extracted_path if extracted_path else 'N/A'}")
                if extracted_path:
                    logger.info(f"  Next step: Manually push to GitLab and create CSV for MTA upload")
            
            # Summary
            if extracted_path:
                logger.info(f"\n✓ Successfully processed {app_name}")
                logger.info(f"  Type: {app_type}")
                logger.info(f"  Extracted to: {extracted_path}")
                if app_type == ApplicationType.JAVA:
                    logger.info(f"  MTA Processing: {'Success' if mta_success else 'Failed'}")
                logger.info("")
                return True
            else:
                logger.warning(f"\n✗ Failed to extract files for {app_name}")
                logger.warning(f"  Reason: {skip_reason if skip_reason else 'Unknown'}\n")
                return False
        
        except KeyboardInterrupt:
            logger.warning(f"Processing interrupted by user for {app_name}")
            raise  # Re-raise to allow main process to handle
        except Exception as e:
            logger.error(f"Unexpected error processing application {app_name}: {type(e).__name__}: {e}")
            import traceback
            logger.debug(f"Traceback: {traceback.format_exc()}")
            skip_reason = f'Unexpected error: {type(e).__name__}'
            self._append_to_summary_csv(app_name, image_url, app_type, metadata, None, skip_reason)
            return False
        
        finally:
            # Cleanup Docker image if enabled
            if self.cleanup:
                try:
                    result = subprocess.run(
                        ['docker', 'rmi', '-f', image_url],
                        capture_output=True,
                        text=True,
                        encoding='utf-8',
                        errors='replace',
                        timeout=30
                    )
                    if result.returncode == 0:
                        logger.info(f"Cleaned up Docker image: {image_url}")
                    else:
                        logger.debug(f"Could not remove image {image_url}: {result.stderr}")
                except subprocess.TimeoutExpired:
                    logger.warning(f"Timeout cleaning up Docker image: {image_url}")
                except Exception as e:
                    logger.debug(f"Error cleaning up Docker image {image_url}: {type(e).__name__}: {e}")
    
    def process_all(self, csv_path: str) -> Dict[str, List[str]]:
        """Process all applications from CSV"""
        
        results = {'successful': [], 'failed': []}
        start_time = datetime.now()
        
        applications = self.read_input_csv(csv_path)
        
        if not applications:
            logger.error("No applications to process")
            return results
        
        logger.info(f"\nStarting processing of {len(applications)} applications")
        logger.info(f"Max concurrent workers: {self.max_workers}\n")
        
        # Process applications with thread pool
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            future_to_app = {
                executor.submit(self.process_application, app['app_name'], app['image_url']): app
                for app in applications
            }
            
            for future in as_completed(future_to_app):
                app = future_to_app[future]
                app_name = app['app_name']
                
                try:
                    success = future.result()
                    
                    if success:
                        results['successful'].append(app_name)
                    else:
                        results['failed'].append(app_name)
                        
                except Exception as e:
                    logger.error(f"Exception processing {app_name}: {e}")
                    results['failed'].append(app_name)
        
        elapsed_time = (datetime.now() - start_time).total_seconds()
        
        # Print summary
        logger.info(f"\n{'='*60}")
        logger.info(f"PROCESSING SUMMARY")
        logger.info(f"{'='*60}")
        logger.info(f"Total applications: {len(applications)}")
        logger.info(f"Successful: {len(results['successful'])}")
        logger.info(f"Failed: {len(results['failed'])}")
        logger.info(f"Total time: {elapsed_time:.2f} seconds")
        logger.info(f"Average time per app: {elapsed_time/len(applications):.2f} seconds")
        logger.info(f"\nConsolidated summary: {self.summary_csv}")
        logger.info(f"Extracted files: {self.output_dir}/ directory")
        
        if results['failed']:
            logger.info(f"\nFailed applications:")
            for app in results['failed']:
                logger.info(f"  - {app}")
        
        return results


def parse_arguments():
    """Parse command line arguments"""
    
    parser = argparse.ArgumentParser(
        description='MTA Analyzer V7 - MTA operations only for Java, files stored for manual GitLab push'
    )
    
    parser.add_argument(
        'mta_url',
        help='MTA base URL (e.g., https://mta.example.com) - Only used for Java apps'
    )
    
    parser.add_argument(
        'input_csv',
        help='Path to input CSV file with app_name and image_url columns'
    )
    
    parser.add_argument(
        '--output-dir',
        help='Directory for extracted artifacts (default: artifacts)',
        default='artifacts'
    )
    
    parser.add_argument(
        '--no-cleanup',
        action='store_true',
        help='Keep Docker images after processing'
    )
    
    parser.add_argument(
        '--max-workers',
        type=int,
        default=4,
        help='Maximum number of concurrent workers (default: 4)'
    )
    
    return parser.parse_args()


def main():
    """Main entry point"""
    
    args = parse_arguments()
    
    logger.info("="*60)
    logger.info("MTA Analyzer V7 - Java MTA + Manual GitLab Workflow")
    logger.info("="*60)
    logger.info(f"MTA URL: {args.mta_url} (Java apps only)")
    logger.info(f"Input CSV: {args.input_csv}")
    logger.info(f"Output Directory: {args.output_dir}")
    logger.info(f"Cleanup: {not args.no_cleanup}")
    logger.info(f"Max Workers: {args.max_workers}")
    logger.info("="*60)
    logger.info("Workflow:")
    logger.info("  - Java: Extract JAR/WAR → MTA (create app, task, upload, submit)")
    logger.info("  - Others: Extract files → Store in subdirectory → Manual GitLab push")
    logger.info("="*60 + "\n")
    
    cleanup_enabled = not args.no_cleanup
    
    try:
        analyzer = MTAAnalyzerV7(
            mta_url=args.mta_url,
            output_dir=args.output_dir,
            cleanup=cleanup_enabled,
            max_workers=args.max_workers
        )
        
        results = analyzer.process_all(args.input_csv)
        
        # Exit with error code if any applications failed
        if results['failed']:
            logger.error("\nSome applications failed to process")
            sys.exit(1)
        else:
            logger.info("\nAll applications processed successfully")
            sys.exit(0)
            
    except KeyboardInterrupt:
        logger.warning("\nProcess interrupted by user")
        sys.exit(130)
    except Exception as e:
        logger.error(f"\nFatal error: {e}")
        sys.exit(1)


if __name__ == '__main__':
    main()