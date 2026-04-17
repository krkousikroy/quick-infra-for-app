#!/usr/bin/env python3
"""
MTA Multi-Language Analyzer
Supports Java (binary), Python, Go, JavaScript, and .NET applications
Automatically detects application type and processes accordingly:
- Java: Binary analysis via MTA
- Others: Source code extraction, GitLab repo creation, and source code analysis via MTA
"""

import os
import sys
import json
import subprocess
import csv
import logging
import argparse
import tempfile
import shutil
import time
import resource
import hashlib
import re
from typing import Dict, List, Optional, Tuple, Any
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError
from threading import Lock, Semaphore, BoundedSemaphore
from pathlib import Path
from datetime import datetime

# Configure logging with UTF-8 encoding
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('mta_multi_language_analyzer.log', encoding='utf-8', errors='replace'),
        logging.StreamHandler(sys.stdout)
    ]
)

# Set UTF-8 encoding for stdout/stderr on Windows
if sys.platform == 'win32':
    try:
        import codecs
        # Windows UTF-8 encoding setup
        sys.stdout = codecs.getwriter('utf-8')(sys.stdout.buffer)
        sys.stderr = codecs.getwriter('utf-8')(sys.stderr.buffer)
    except:
        pass  # Fallback if encoding setup fails

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

# Create thread-safe logger
base_logger = logging.getLogger(__name__)
logger = ThreadSafeLogger(base_logger)


class ApplicationType:
    """Application type constants"""
    JAVA = "java"
    PYTHON = "python"
    GO = "go"
    JAVASCRIPT = "javascript"
    DOTNET = "dotnet"
    INFRASTRUCTURE = "infrastructure"  # nginx, apache, redis, etc.
    DATABASE = "database"  # postgres, mysql, mongodb, etc.
    UNKNOWN = "unknown"


class MTAMultiLanguageAnalyzer:
    """Main class for multi-language MTA analysis automation"""
    
    def __init__(self, mta_url: str, gitlab_url: str, gitlab_token: str,
                 gitlab_group: Optional[str] = None, output_dir: str = 'artifacts',
                 cleanup: bool = True, max_workers: int = 4, discovery_only: bool = False):
        """
        Initialize MTA Multi-Language Analyzer
        
        Args:
            mta_url: Base URL of MTA installation
            gitlab_url: GitLab instance URL
            gitlab_token: GitLab personal access token
            gitlab_group: GitLab group/namespace for repositories (optional)
            output_dir: Directory to store extracted artifacts
            cleanup: Whether to cleanup Docker images/containers and artifacts
            max_workers: Maximum number of concurrent threads
            discovery_only: If True, only gather info without MTA processing
        """
        self.mta_url = mta_url.rstrip('/') if mta_url else ''
        self.gitlab_url = gitlab_url.rstrip('/') if gitlab_url else ''
        self.gitlab_token = gitlab_token if gitlab_token else ''
        self.gitlab_group = gitlab_group
        self.output_dir = output_dir
        self.cleanup = cleanup
        self.max_workers = max_workers
        self.discovery_only = discovery_only
        self.processed_images = []
        self.images_lock = Lock()
        os.makedirs(output_dir, exist_ok=True)
        
        if discovery_only:
            logger.info("Running in DISCOVERY ONLY mode - MTA processing will be skipped")
    
    def _sanitize_name(self, name: str, max_length: int = 50) -> str:
        """
        Sanitize application name for use in file paths and folder names
        Handles long names, special characters, and cross-platform compatibility
        
        Args:
            name: Original application name
            max_length: Maximum length for the sanitized name
            
        Returns:
            Safe name for file system use
        """
        import re
        import hashlib
        
        # Remove or replace problematic characters
        # Windows forbidden: < > : " / \ | ? *
        # Also remove other special chars for safety
        safe_name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name)
        
        # Replace multiple underscores with single
        safe_name = re.sub(r'_+', '_', safe_name)
        
        # Remove leading/trailing dots and spaces (Windows issue)
        safe_name = safe_name.strip('. ')
        
        # If name is too long, truncate and add hash for uniqueness
        if len(safe_name) > max_length:
            # Create hash of original name for uniqueness
            name_hash = hashlib.md5(name.encode('utf-8')).hexdigest()[:8]
            # Keep first part of name and add hash
            safe_name = safe_name[:max_length-9] + '_' + name_hash
        
        # Ensure name is not empty
        if not safe_name:
            safe_name = f"app_{hashlib.md5(name.encode('utf-8')).hexdigest()[:12]}"
        
        # Ensure name doesn't end with dot or space (Windows)
        safe_name = safe_name.rstrip('. ')
        
        return safe_name
    
    def _get_safe_path(self, base_dir: str, app_name: str, *subdirs) -> str:
        """
        Create safe cross-platform path with proper length handling
        
        Args:
            base_dir: Base directory
            app_name: Application name (will be sanitized)
            *subdirs: Additional subdirectory components
            
        Returns:
            Safe absolute path
        """
        # Sanitize app name
        safe_app_name = self._sanitize_name(app_name)
        
        # Build path components
        path_parts = [base_dir, safe_app_name] + list(subdirs)
        
        # Join with OS-specific separator
        full_path = os.path.join(*path_parts)
        
        # Convert to absolute path
        full_path = os.path.abspath(full_path)
        
        # On Windows, check path length (260 char limit without long path support)
        if sys.platform == 'win32' and len(full_path) > 240:
            # Use shorter hash-based directory structure
            app_hash = hashlib.md5(app_name.encode('utf-8')).hexdigest()[:12]
            path_parts = [base_dir, app_hash] + list(subdirs)
            full_path = os.path.abspath(os.path.join(*path_parts))
            logger.debug(f"Using hash-based path for long name: {full_path}")
        
        return full_path

    def read_input_csv(self, csv_path: str) -> List[Dict[str, str]]:
        """Read applications from CSV file"""
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
            
            logger.info(f"Read {len(applications)} applications from {csv_path}")
            return applications
            
        except Exception as e:
            logger.error(f"Failed to read CSV file: {e}")
            return []
    
    def pull_docker_image(self, image_url: str) -> bool:
        """Pull Docker image"""
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
                
        except Exception as e:
            logger.error(f"Error pulling image: {e}")
            return False
    
    def detect_application_type(self, image_url: str) -> Tuple[str, Dict[str, Any]]:
        """
        Detect application type from Docker image metadata with enhanced fallback strategies
        Includes infrastructure/database detection
        
        Returns:
            Tuple of (application_type, metadata_with_detection_details)
        """
        logger.info(f"Detecting application type for: {image_url}")
        
        metadata = {
            'entrypoint': [],
            'cmd': [],
            'env': {},
            'labels': {},
            'workdir': '',
            'detected_files': [],
            'detection_method': 'unknown',
            'confidence_score': 0,
            'detection_details': [],
            'image_info': {},
            'fallback_attempts': [],
            'infrastructure_subtype': ''
        }
        
        try:
            # Inspect Docker image
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
                    
                    metadata['entrypoint'] = config.get('Entrypoint') or []
                    metadata['cmd'] = config.get('Cmd') or []
                    metadata['workdir'] = config.get('WorkingDir', '')
                    metadata['labels'] = config.get('Labels') or {}
                    
                    # Extract image info
                    metadata['image_info'] = {
                        'id': inspect_data[0].get('Id', '')[:12],
                        'created': inspect_data[0].get('Created', ''),
                        'architecture': inspect_data[0].get('Architecture', ''),
                        'os': inspect_data[0].get('Os', ''),
                        'size': inspect_data[0].get('Size', 0)
                    }
                    
                    # Extract ENV variables
                    env_list = config.get('Env') or []
                    for env_var in env_list:
                        if '=' in env_var:
                            key, value = env_var.split('=', 1)
                            metadata['env'][key] = value
            
            # Extract additional metadata (ports, volumes, etc.)
            self._extract_additional_metadata(image_url, metadata)
            
            # Strategy 0: Check for infrastructure/database first
            app_type = self._detect_infrastructure_type(metadata)
            if app_type:
                metadata['detection_method'] = 'infrastructure_detection'
                metadata['confidence_score'] = 95
                logger.info(f"Detected infrastructure/database: {app_type} ({metadata.get('infrastructure_subtype', '')})")
                return app_type, metadata
            
            # Strategy 1: Analyze metadata for application types
            app_type = self._analyze_metadata_for_type(metadata)
            if app_type != ApplicationType.UNKNOWN:
                metadata['detection_method'] = 'metadata_analysis'
                logger.info(f"Detected via metadata: {app_type}")
            else:
                metadata['fallback_attempts'].append('metadata_analysis_failed')
            
            # Strategy 2: Scan container filesystem if still unknown
            if app_type == ApplicationType.UNKNOWN:
                app_type = self._scan_container_for_type(image_url, metadata)
                if app_type != ApplicationType.UNKNOWN:
                    metadata['detection_method'] = 'filesystem_scan'
                    logger.info(f"Detected via filesystem: {app_type}")
                else:
                    metadata['fallback_attempts'].append('filesystem_scan_failed')
            
            # Strategy 3: Image name heuristics if still unknown
            if app_type == ApplicationType.UNKNOWN:
                app_type = self._detect_from_image_name(image_url, metadata)
                if app_type != ApplicationType.UNKNOWN:
                    metadata['detection_method'] = 'image_name_heuristics'
                    logger.info(f"Detected via image name: {app_type}")
                else:
                    metadata['fallback_attempts'].append('image_name_heuristics_failed')
            
            # Strategy 4: Base image detection if still unknown
            if app_type == ApplicationType.UNKNOWN:
                app_type = self._detect_from_base_image(metadata)
                if app_type != ApplicationType.UNKNOWN:
                    metadata['detection_method'] = 'base_image_detection'
                    logger.info(f"Detected via base image: {app_type}")
                else:
                    metadata['fallback_attempts'].append('base_image_detection_failed')
            
            logger.info(f"Final detected application type: {app_type}")
            return app_type, metadata
            
        except Exception as e:
            logger.error(f"Error detecting application type: {e}")
            return ApplicationType.UNKNOWN, metadata
    
    def _detect_infrastructure_type(self, metadata: Dict[str, Any]) -> str:
        """
        Detect if image is infrastructure/database related
        Returns specific infrastructure type or empty string
        """
        
        # Combine all metadata for analysis
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
        
        # Infrastructure patterns
        infrastructure_patterns = {
            'nginx': ['nginx', 'nginx.conf', '/etc/nginx'],
            'apache': ['apache', 'httpd', 'apache2', '/etc/apache'],
            'redis': ['redis-server', 'redis.conf', 'redis-cli'],
            'memcached': ['memcached', 'memcache'],
            'rabbitmq': ['rabbitmq', 'rabbitmq-server'],
            'kafka': ['kafka', 'zookeeper', 'kafka-server'],
            'elasticsearch': ['elasticsearch', 'elastic'],
            'haproxy': ['haproxy', 'haproxy.cfg'],
            'traefik': ['traefik'],
            'envoy': ['envoy'],
        }
        
        # Database patterns
        database_patterns = {
            'postgres': ['postgres', 'postgresql', 'psql'],
            'mysql': ['mysql', 'mysqld', 'mariadb'],
            'mongodb': ['mongo', 'mongod'],
            'cassandra': ['cassandra'],
            'couchdb': ['couchdb'],
            'influxdb': ['influxdb'],
            'neo4j': ['neo4j'],
            'oracle': ['oracle', 'sqlplus'],
            'mssql': ['mssql', 'sqlserver'],
        }
        
        # Check infrastructure
        for infra_type, patterns in infrastructure_patterns.items():
            if any(pattern in combined for pattern in patterns):
                metadata['infrastructure_subtype'] = infra_type
                metadata['detection_details'].append(f"Infrastructure: {infra_type}")
                return ApplicationType.INFRASTRUCTURE
        
        # Check database
        for db_type, patterns in database_patterns.items():
            if any(pattern in combined for pattern in patterns):
                metadata['infrastructure_subtype'] = f"database-{db_type}"
                metadata['detection_details'].append(f"Database: {db_type}")
                return ApplicationType.DATABASE
        
        return ""
    
    def _extract_additional_metadata(self, image_url: str, metadata: Dict[str, Any]):
        """
        Extract additional useful information from Docker image
        Enhances discovery with ports, volumes, users, health checks, etc.
        """
        
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
                inspect_data = json.loads(result.stdout)
                
                if inspect_data and len(inspect_data) > 0:
                    config = inspect_data[0].get('Config', {})
                    container_config = inspect_data[0].get('ContainerConfig', {})
                    
                    # Exposed ports
                    exposed_ports = config.get('ExposedPorts', {})
                    if exposed_ports:
                        metadata['exposed_ports'] = list(exposed_ports.keys())
                    
                    # Volumes
                    volumes = config.get('Volumes', {})
                    if volumes:
                        metadata['volumes'] = list(volumes.keys())
                    
                    # User
                    user = config.get('User', '')
                    if user:
                        metadata['user'] = user
                    
                    # Health check
                    healthcheck = config.get('Healthcheck', {})
                    if healthcheck:
                        metadata['healthcheck'] = {
                            'test': healthcheck.get('Test', []),
                            'interval': healthcheck.get('Interval', 0),
                            'timeout': healthcheck.get('Timeout', 0),
                            'retries': healthcheck.get('Retries', 0)
                        }
                    
                    # Stop signal
                    stop_signal = config.get('StopSignal', '')
                    if stop_signal:
                        metadata['stop_signal'] = stop_signal
                    
                    # Shell
                    shell = config.get('Shell', [])
                    if shell:
                        metadata['shell'] = shell
                    
                    # Image layers count
                    root_fs = inspect_data[0].get('RootFS', {})
                    layers = root_fs.get('Layers', [])
                    metadata['layer_count'] = len(layers)
                    
                    # Parent image
                    parent = inspect_data[0].get('Parent', '')
                    if parent:
                        metadata['parent_image'] = parent[:12]
                    
                    # Docker version
                    docker_version = inspect_data[0].get('DockerVersion', '')
                    if docker_version:
                        metadata['docker_version'] = docker_version
                    
                    # Author
                    author = inspect_data[0].get('Author', '')
                    if author:
                        metadata['author'] = author
                    
                    logger.debug(f"Extracted additional metadata: ports={metadata.get('exposed_ports', [])}, volumes={metadata.get('volumes', [])}")
                    
        except Exception as e:
            logger.debug(f"Could not extract additional metadata: {e}")
    
    def _analyze_metadata_for_type(self, metadata: Dict[str, Any]) -> str:
        """
        Analyze metadata to determine application type using weighted scoring system
        More dynamic and efficient detection with confidence scoring
        """
        
        # Combine all metadata into searchable strings
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
        
        workdir = metadata.get('workdir', '').lower()
        
        # Enhanced detection patterns with weights
        detection_patterns = {
            ApplicationType.JAVA: {
                'high_confidence': ['.jar', '.war', '.ear', 'java -jar', 'catalina', 'jboss', 'wildfly'],
                'medium_confidence': ['java', 'jre', 'jdk', 'tomcat', 'spring-boot', 'maven', 'gradle'],
                'low_confidence': ['openjdk', 'adoptopenjdk', 'java_home'],
                'paths': ['/opt/jboss', '/opt/wildfly', '/usr/local/tomcat', '/deployments'],
                'env_vars': ['JAVA_HOME', 'CATALINA_HOME', 'JBOSS_HOME']
            },
            ApplicationType.PYTHON: {
                'high_confidence': ['python3', 'pip install', 'requirements.txt', 'django-admin', 'flask run', 'uvicorn', 'gunicorn'],
                'medium_confidence': ['python', 'pip', 'django', 'flask', 'fastapi', 'poetry', 'pipenv'],
                'low_confidence': ['py', 'python2', 'virtualenv'],
                'paths': ['/usr/local/lib/python', '/opt/python', '/app/venv'],
                'env_vars': ['PYTHONPATH', 'VIRTUAL_ENV', 'DJANGO_SETTINGS_MODULE']
            },
            ApplicationType.GO: {
                'high_confidence': ['go run', 'go build', 'go.mod', 'go.sum', '/go/bin'],
                'medium_confidence': ['golang', 'go get', '/go/src', 'go install'],
                'low_confidence': ['/usr/local/go', 'gopath'],
                'paths': ['/go/src', '/go/bin', '/usr/local/go'],
                'env_vars': ['GOPATH', 'GOROOT', 'GO111MODULE']
            },
            ApplicationType.JAVASCRIPT: {
                'high_confidence': ['npm start', 'npm run', 'node index', 'yarn start', 'package.json', 'node_modules'],
                'medium_confidence': ['node', 'npm', 'yarn', 'nodejs', 'express', 'react', 'vue', 'angular'],
                'low_confidence': ['javascript', 'js', 'typescript'],
                'paths': ['/usr/local/lib/node_modules', '/app/node_modules', '/opt/nodejs'],
                'env_vars': ['NODE_ENV', 'NPM_CONFIG_PREFIX', 'NODE_PATH']
            },
            ApplicationType.DOTNET: {
                'high_confidence': ['dotnet run', 'dotnet exec', '.dll', 'aspnetcore', 'dotnet.exe'],
                'medium_confidence': ['dotnet', '.net', 'aspnet', 'csharp', 'nuget'],
                'low_confidence': ['microsoft', 'msft', 'c#'],
                'paths': ['/app/bin', '/opt/dotnet', '/usr/share/dotnet'],
                'env_vars': ['DOTNET_ROOT', 'ASPNETCORE_ENVIRONMENT', 'DOTNET_RUNNING_IN_CONTAINER']
            }
        }
        
        # Calculate confidence scores for each type
        scores = {}
        for app_type, patterns in detection_patterns.items():
            score = 0
            details = []
            
            # Check high confidence patterns (weight: 10)
            for pattern in patterns['high_confidence']:
                if pattern in all_commands:
                    score += 10
                    details.append(f"CMD:{pattern}")
                elif pattern in env_str:
                    score += 8
                    details.append(f"ENV:{pattern}")
                elif pattern in labels_str:
                    score += 7
                    details.append(f"LABEL:{pattern}")
            
            # Check medium confidence patterns (weight: 5)
            for pattern in patterns['medium_confidence']:
                if pattern in all_commands:
                    score += 5
                    details.append(f"CMD:{pattern}")
                elif pattern in env_str:
                    score += 4
                    details.append(f"ENV:{pattern}")
            
            # Check low confidence patterns (weight: 2)
            for pattern in patterns['low_confidence']:
                if pattern in all_commands or pattern in env_str:
                    score += 2
                    details.append(f"WEAK:{pattern}")
            
            # Check paths (weight: 6)
            for path in patterns['paths']:
                if path in workdir or path in all_commands:
                    score += 6
                    details.append(f"PATH:{path}")
            
            # Check environment variables (weight: 7)
            for env_var in patterns['env_vars']:
                if env_var.lower() in env_str:
                    score += 7
                    details.append(f"ENVVAR:{env_var}")
            
            if score > 0:
                scores[app_type] = {'score': score, 'details': details}
                logger.debug(f"{app_type} score: {score} - {', '.join(details[:3])}")
        
        # Return type with highest score (minimum threshold: 5)
        if scores:
            best_match = max(scores.items(), key=lambda x: x[1]['score'])
            if best_match[1]['score'] >= 5:
                logger.info(f"Detected {best_match[0]} with confidence score {best_match[1]['score']}")
                return best_match[0]
        
        return ApplicationType.UNKNOWN
    
    def _scan_container_for_type(self, image_url: str, metadata: Dict[str, Any]) -> str:
        """
        Scan container filesystem to detect application type
        Enhanced with parallel scanning, threading, and resource limits
        """
        
        # Priority-based file patterns with scoring
        detection_config = {
            ApplicationType.JAVA: {
                'critical': ['*.war', '*.ear'],
                'high': ['*.jar'],
                'medium': ['pom.xml', 'build.gradle', 'gradlew'],
                'paths': ['/deployments', '/opt/jboss', '/opt/wildfly', '/app', '/opt/app']
            },
            ApplicationType.PYTHON: {
                'critical': ['requirements.txt', 'pyproject.toml', 'Pipfile'],
                'high': ['setup.py', 'manage.py', 'wsgi.py', 'asgi.py'],
                'medium': ['*.py'],
                'paths': ['/app', '/usr/src/app', '/opt/app', '/code']
            },
            ApplicationType.GO: {
                'critical': ['go.mod', 'go.sum'],
                'high': ['main.go'],
                'medium': ['*.go'],
                'paths': ['/app', '/go/src', '/usr/local/go/src', '/opt/app']
            },
            ApplicationType.JAVASCRIPT: {
                'critical': ['package.json', 'package-lock.json', 'yarn.lock'],
                'high': ['server.js', 'index.js', 'app.js'],
                'medium': ['node_modules'],
                'paths': ['/app', '/usr/src/app', '/opt/app', '/var/www']
            },
            ApplicationType.DOTNET: {
                'critical': ['*.csproj', '*.sln', '*.fsproj', '*.vbproj'],
                'high': ['*.dll', 'appsettings.json'],
                'medium': ['*.cs', '*.fs'],
                'paths': ['/app', '/src', '/opt/app', '/application']
            }
        }
        
        # Use parallel scanning with thread pool and resource limits
        scores = {}
        scores_lock = Lock()
        
        # Limit concurrent Docker operations
        max_concurrent_scans = min(3, len(detection_config))
        scan_semaphore = BoundedSemaphore(max_concurrent_scans)
        
        def scan_app_type(app_type: str, config: Dict[str, Any]) -> Tuple[str, int, List[str]]:
            """Scan for a specific application type with resource limits"""
            with scan_semaphore:
                try:
                    # Set resource limits for subprocess
                    def set_limits():
                        # Limit memory to 512MB
                        try:
                            resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
                        except:
                            pass  # Not all systems support this
                    
                    type_score = 0
                    found_files = []
                    search_paths = config.get('paths', ['/'])
                    
                    for priority, patterns in [('critical', config.get('critical', [])),
                                              ('high', config.get('high', [])),
                                              ('medium', config.get('medium', []))]:
                        
                        if not patterns or type_score >= 20:  # Early exit
                            break
                        
                        # Build efficient find command
                        pattern_args = []
                        for i, pattern in enumerate(patterns):
                            if i > 0:
                                pattern_args.append('-o')
                            pattern_args.extend(['-name', pattern])
                        
                        # Search in priority paths with depth and result limits
                        for search_path in search_paths[:2]:
                            find_parts = ['find', search_path, '-maxdepth', '4', '-type', 'f', '\\(']
                            find_parts.extend(pattern_args)
                            find_parts.extend(['\\)', '2>/dev/null', '|', 'head', '-10'])
                            
                            cmd = ' '.join(find_parts)
                            
                            try:
                                result = subprocess.run(
                                    ['docker', 'run', '--rm',
                                     '--memory=256m',  # Docker memory limit
                                     '--cpus=0.5',  # Docker CPU limit
                                     '--entrypoint', 'sh', image_url, '-c', cmd],
                                    capture_output=True,
                                    text=True,
                                    encoding='utf-8',
                                    errors='replace',
                                    timeout=15,  # Reduced timeout
                                    preexec_fn=set_limits if os.name != 'nt' else None
                                )
                                
                                if result.returncode == 0 and result.stdout.strip():
                                    files = [f.strip() for f in result.stdout.strip().split('\n') if f.strip()]
                                    found_files.extend(files)
                                    
                                    # Score based on priority
                                    if priority == 'critical':
                                        type_score += len(files) * 10
                                    elif priority == 'high':
                                        type_score += len(files) * 5
                                    else:
                                        type_score += len(files) * 2
                                    
                                    # Early exit if we found critical files
                                    if priority == 'critical' and files:
                                        logger.debug(f"Found {app_type} critical files: {files[:2]}")
                                        break
                            
                            except subprocess.TimeoutExpired:
                                logger.debug(f"Scan timeout for {app_type} in {search_path}")
                                break
                            except Exception as e:
                                logger.debug(f"Scan error for {app_type}: {e}")
                                break
                    
                    return app_type, type_score, found_files[:5]
                
                except Exception as e:
                    logger.debug(f"Error scanning {app_type}: {e}")
                    return app_type, 0, []
        
        try:
            # Parallel scanning with thread pool
            with ThreadPoolExecutor(max_workers=max_concurrent_scans) as executor:
                # Submit all scan tasks
                future_to_type = {
                    executor.submit(scan_app_type, app_type, config): app_type
                    for app_type, config in detection_config.items()
                }
                
                # Collect results with timeout
                for future in as_completed(future_to_type, timeout=30):
                    try:
                        app_type, type_score, found_files = future.result(timeout=5)
                        
                        if type_score > 0:
                            with scores_lock:
                                scores[app_type] = type_score
                                metadata['detected_files'].extend(found_files)
                                logger.debug(f"{app_type} filesystem score: {type_score}")
                    
                    except TimeoutError:
                        app_type = future_to_type[future]
                        logger.warning(f"Scan timeout for {app_type}")
                    except Exception as e:
                        app_type = future_to_type[future]
                        logger.debug(f"Error processing {app_type}: {e}")
            
            # Return type with highest score
            if scores:
                best_match = max(scores.items(), key=lambda x: x[1])
                logger.info(f"Filesystem scan detected {best_match[0]} with score {best_match[1]}")
                return best_match[0]
        
        except TimeoutError:
            logger.warning(f"Overall filesystem scan timed out")
        except Exception as e:
            logger.warning(f"Error in parallel scanning: {e}")
        
        return ApplicationType.UNKNOWN
    def _detect_from_image_name(self, image_url: str, metadata: Dict[str, Any]) -> str:
        """
        Detect application type from Docker image name/URL
        Fallback strategy when metadata is insufficient
        """
        image_lower = image_url.lower()
        
        # Image name patterns
        patterns = {
            ApplicationType.JAVA: ['java', 'jdk', 'jre', 'openjdk', 'tomcat', 'wildfly', 'jboss', 'spring'],
            ApplicationType.PYTHON: ['python', 'django', 'flask', 'fastapi', 'py'],
            ApplicationType.GO: ['golang', 'go'],
            ApplicationType.JAVASCRIPT: ['node', 'nodejs', 'npm', 'javascript'],
            ApplicationType.DOTNET: ['dotnet', 'aspnet', 'csharp', 'netcore']
        }
        
        for app_type, keywords in patterns.items():
            if any(keyword in image_lower for keyword in keywords):
                metadata['detection_details'].append(f"Image name contains '{keywords[0]}'")
                metadata['confidence_score'] = 30  # Low confidence
                logger.info(f"Image name suggests {app_type}")
                return app_type
        
        return ApplicationType.UNKNOWN
    
    def _detect_from_base_image(self, metadata: Dict[str, Any]) -> str:
        """
        Detect application type from base image information in labels
        Fallback strategy using Docker labels
        """
        labels = metadata.get('labels', {})
        
        # Check common base image labels
        base_image_keys = ['org.opencontainers.image.base.name', 'base-image', 'from']
        
        for key in base_image_keys:
            base_image = labels.get(key, '').lower()
            if base_image:
                # Analyze base image name
                if any(keyword in base_image for keyword in ['java', 'jdk', 'jre', 'openjdk']):
                    metadata['detection_details'].append(f"Base image: {base_image}")
                    metadata['confidence_score'] = 25
                    return ApplicationType.JAVA
                elif any(keyword in base_image for keyword in ['python', 'py']):
                    metadata['detection_details'].append(f"Base image: {base_image}")
                    metadata['confidence_score'] = 25
                    return ApplicationType.PYTHON
                elif 'golang' in base_image or base_image.startswith('go:'):
                    metadata['detection_details'].append(f"Base image: {base_image}")
                    metadata['confidence_score'] = 25
                    return ApplicationType.GO
                elif any(keyword in base_image for keyword in ['node', 'nodejs']):
                    metadata['detection_details'].append(f"Base image: {base_image}")
                    metadata['confidence_score'] = 25
                    return ApplicationType.JAVASCRIPT
                elif any(keyword in base_image for keyword in ['dotnet', 'aspnet']):
                    metadata['detection_details'].append(f"Base image: {base_image}")
                    metadata['confidence_score'] = 25
                    return ApplicationType.DOTNET
        
        return ApplicationType.UNKNOWN
    
    def export_detection_report(self, app_name: str, image_url: str, app_type: str,
                               metadata: Dict[str, Any], output_dir: str = 'reports') -> str:
        """
        Export detailed detection report as CSV for each image with robust error handling
        
        Returns:
            Path to the generated CSV file
        """
        try:
            os.makedirs(output_dir, exist_ok=True)
        except Exception as e:
            logger.warning(f"Failed to create reports directory: {e}")
            output_dir = '.'  # Fallback to current directory
        
        # Sanitize app name for filename (handle special characters)
        safe_name = "".join(c if c.isalnum() or c in ('-', '_') else '_' for c in app_name)
        if not safe_name:
            safe_name = f"app_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        
        csv_path = os.path.join(output_dir, f"{safe_name}_detection_report.csv")
        
        try:
            with open(csv_path, 'w', newline='', encoding='utf-8', errors='replace') as csvfile:
                writer = csv.writer(csvfile)
                
                # Header
                writer.writerow(['Category', 'Key', 'Value'])
                
                # Basic Information
                writer.writerow(['Basic Info', 'Application Name', app_name])
                writer.writerow(['Basic Info', 'Image URL', image_url])
                writer.writerow(['Basic Info', 'Detected Type', app_type])
                writer.writerow(['Basic Info', 'Detection Method', metadata.get('detection_method', 'unknown')])
                writer.writerow(['Basic Info', 'Confidence Score', metadata.get('confidence_score', 0)])
                writer.writerow(['Basic Info', 'Timestamp', datetime.now().isoformat()])
                writer.writerow([])
                
                # Image Information
                image_info = metadata.get('image_info', {})
                if image_info:
                    writer.writerow(['Image Info', 'Image ID', image_info.get('id', '')])
                    writer.writerow(['Image Info', 'Created', image_info.get('created', '')])
                    writer.writerow(['Image Info', 'Architecture', image_info.get('architecture', '')])
                    writer.writerow(['Image Info', 'OS', image_info.get('os', '')])
                    writer.writerow(['Image Info', 'Size (bytes)', image_info.get('size', 0)])
                    writer.writerow([])
                
                # Docker Metadata
                writer.writerow(['Docker Metadata', 'ENTRYPOINT', ' '.join(metadata.get('entrypoint', []))])
                writer.writerow(['Docker Metadata', 'CMD', ' '.join(metadata.get('cmd', []))])
                writer.writerow(['Docker Metadata', 'WORKDIR', metadata.get('workdir', '')])
                writer.writerow([])
                
                # Environment Variables
                env_vars = metadata.get('env', {})
                if env_vars:
                    writer.writerow(['Environment Variables', 'Count', len(env_vars)])
                    for key, value in list(env_vars.items())[:20]:  # Limit to first 20
                        writer.writerow(['Environment Variables', key, value[:100]])  # Truncate long values
                    if len(env_vars) > 20:
                        writer.writerow(['Environment Variables', '...', f'({len(env_vars) - 20} more variables)'])
                    writer.writerow([])
                
                # Labels
                labels = metadata.get('labels', {})
                if labels:
                    writer.writerow(['Labels', 'Count', len(labels)])
                    for key, value in list(labels.items())[:20]:  # Limit to first 20
                        writer.writerow(['Labels', key, value[:100]])
                    if len(labels) > 20:
                        writer.writerow(['Labels', '...', f'({len(labels) - 20} more labels)'])
                    writer.writerow([])
                
                # Detected Files
                detected_files = metadata.get('detected_files', [])
                if detected_files:
                    writer.writerow(['Detected Files', 'Count', len(detected_files)])
                    for i, file_path in enumerate(detected_files[:30], 1):  # Limit to first 30
                        writer.writerow(['Detected Files', f'File {i}', file_path])
                    if len(detected_files) > 30:
                        writer.writerow(['Detected Files', '...', f'({len(detected_files) - 30} more files)'])
                    writer.writerow([])
                
                # Detection Details
                detection_details = metadata.get('detection_details', [])
                if detection_details:
                    writer.writerow(['Detection Details', 'Count', len(detection_details)])
                    for i, detail in enumerate(detection_details, 1):
                        writer.writerow(['Detection Details', f'Detail {i}', detail])
                    writer.writerow([])
                
                # Fallback Attempts
                fallback_attempts = metadata.get('fallback_attempts', [])
                if fallback_attempts:
                    writer.writerow(['Fallback Attempts', 'Count', len(fallback_attempts)])
                    for i, attempt in enumerate(fallback_attempts, 1):
                        writer.writerow(['Fallback Attempts', f'Attempt {i}', attempt])
                    writer.writerow([])
                
                # Summary Statistics
                writer.writerow(['Summary', 'Total ENV Variables', len(env_vars)])
                writer.writerow(['Summary', 'Total Labels', len(labels)])
                writer.writerow(['Summary', 'Total Detected Files', len(detected_files)])
                writer.writerow(['Summary', 'Detection Confidence', 
                               'High' if metadata.get('confidence_score', 0) >= 50 else 
                               'Medium' if metadata.get('confidence_score', 0) >= 25 else 'Low'])
            
            logger.info(f"Detection report exported to: {csv_path}")
            return csv_path
            
        except Exception as e:
            logger.error(f"Failed to export detection report: {e}")
            return ""
    
    def extract_source_code(self, image_url: str, app_name: str, app_type: str, 
                           metadata: Dict[str, Any]) -> Optional[str]:
        """
        Extract source code from container for non-Java applications
        
        Returns:
            Path to extracted source code directory
        """
        logger.info(f"Extracting source code for {app_type} application: {app_name}")
        
        app_dir = os.path.join(self.output_dir, app_name)
        os.makedirs(app_dir, exist_ok=True)
        
        # Determine source code locations based on app type and metadata
        search_paths = self._get_source_code_paths(app_type, metadata)
        
        container_id = None
        try:
            # Create container
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
            
            # Try each search path
            extracted = False
            for search_path in search_paths:
                logger.info(f"Attempting to extract from: {search_path}")
                
                # Copy directory from container
                dest_path = os.path.join(app_dir, 'source')
                result = subprocess.run(
                    ['docker', 'cp', f"{container_id}:{search_path}", dest_path],
                    capture_output=True,
                    text=True,
                    encoding='utf-8',
                    errors='replace'
                )
                
                if result.returncode == 0:
                    # Verify we got actual source files
                    if self._verify_source_code(dest_path, app_type):
                        logger.info(f"Successfully extracted source code from {search_path}")
                        extracted = True
                        break
                    else:
                        # Clean up invalid extraction
                        if os.path.exists(dest_path):
                            shutil.rmtree(dest_path)
            
            if not extracted:
                logger.error(f"Failed to extract source code for {app_name}")
                return None
            
            return os.path.join(app_dir, 'source')
            
        except Exception as e:
            logger.error(f"Error extracting source code: {e}")
            return None
            
        finally:
            # Cleanup container
            if container_id:
                subprocess.run(
                    ['docker', 'rm', '-f', container_id],
                    capture_output=True,
                    stderr=subprocess.DEVNULL
                )
    
    def _get_source_code_paths(self, app_type: str, metadata: Dict[str, Any]) -> List[str]:
        """
        Get potential source code paths based on app type with intelligent prioritization
        More dynamic path discovery using metadata analysis
        """
        
        paths_with_priority = []  # List of (path, priority) tuples
        
        # Priority 1: Paths from detected files (highest confidence)
        detected_paths = set()
        for file_path in metadata.get('detected_files', []):
            dir_path = os.path.dirname(file_path)
            if dir_path and dir_path not in ['/', '/usr', '/usr/local']:
                detected_paths.add(dir_path)
                # Also add parent directory if it looks like an app directory
                parent = os.path.dirname(dir_path)
                if parent and any(name in parent for name in ['/app', '/src', '/code', '/opt']):
                    detected_paths.add(parent)
        
        for path in detected_paths:
            paths_with_priority.append((path, 100))  # Highest priority
        
        # Priority 2: Working directory from Docker metadata
        workdir = metadata.get('workdir', '').strip()
        if workdir and workdir not in ['/', '/root', '/tmp']:
            paths_with_priority.append((workdir, 90))
        
        # Priority 3: Paths extracted from ENTRYPOINT/CMD
        cmd_paths = self._extract_paths_from_commands(metadata)
        for path in cmd_paths:
            if path not in [p[0] for p in paths_with_priority]:
                paths_with_priority.append((path, 80))
        
        # Priority 4: Paths from environment variables
        env_paths = self._extract_paths_from_env(metadata, app_type)
        for path in env_paths:
            if path not in [p[0] for p in paths_with_priority]:
                paths_with_priority.append((path, 70))
        
        # Priority 5: Framework-specific conventional paths
        conventional_paths = {
            ApplicationType.PYTHON: [
                '/app', '/usr/src/app', '/opt/app', '/code', '/application',
                '/usr/local/app', '/home/app', '/var/app'
            ],
            ApplicationType.GO: [
                '/app', '/go/src/app', '/go/src', '/usr/local/go/src',
                '/opt/app', '/home/go/app'
            ],
            ApplicationType.JAVASCRIPT: [
                '/app', '/usr/src/app', '/opt/app', '/var/www', '/var/www/html',
                '/home/node/app', '/usr/app', '/application'
            ],
            ApplicationType.DOTNET: [
                '/app', '/src', '/opt/app', '/application', '/usr/src/app',
                '/home/dotnet/app'
            ]
        }
        
        default_paths = conventional_paths.get(app_type, ['/app', '/opt/app', '/usr/src/app'])
        for path in default_paths:
            if path not in [p[0] for p in paths_with_priority]:
                paths_with_priority.append((path, 50))
        
        # Priority 6: Generic fallback paths
        fallback_paths = ['/opt', '/usr/local', '/home']
        for path in fallback_paths:
            if path not in [p[0] for p in paths_with_priority]:
                paths_with_priority.append((path, 10))
        
        # Sort by priority (highest first) and return paths
        paths_with_priority.sort(key=lambda x: x[1], reverse=True)
        sorted_paths = [path for path, _ in paths_with_priority]
        
        logger.debug(f"Source code search paths (prioritized): {sorted_paths[:5]}")
        return sorted_paths
    
    def _extract_paths_from_commands(self, metadata: Dict[str, Any]) -> List[str]:
        """Extract potential application paths from ENTRYPOINT and CMD"""
        paths = set()
        
        all_commands = metadata.get('entrypoint', []) + metadata.get('cmd', [])
        
        for cmd in all_commands:
            if not isinstance(cmd, str):
                continue
            
            # Look for absolute paths in commands
            import re
            # Match paths like /app/server.py, /opt/myapp/main.go, etc.
            path_pattern = r'(/[\w\-./]+/[\w\-./]+)'
            matches = re.findall(path_pattern, cmd)
            
            for match in matches:
                # Extract directory path
                if os.path.dirname(match):
                    dir_path = os.path.dirname(match)
                    # Filter out system paths
                    if not any(dir_path.startswith(p) for p in ['/bin', '/usr/bin', '/lib', '/etc']):
                        paths.add(dir_path)
        
        return list(paths)
    
    def _extract_paths_from_env(self, metadata: Dict[str, Any], app_type: str) -> List[str]:
        """Extract potential application paths from environment variables"""
        paths = set()
        env_vars = metadata.get('env', {})
        
        # Environment variables that commonly contain app paths
        path_env_vars = {
            ApplicationType.PYTHON: ['PYTHONPATH', 'APP_HOME', 'APPLICATION_PATH'],
            ApplicationType.GO: ['GOPATH', 'APP_HOME', 'APPLICATION_PATH'],
            ApplicationType.JAVASCRIPT: ['NODE_PATH', 'APP_HOME', 'APPLICATION_PATH'],
            ApplicationType.DOTNET: ['DOTNET_ROOT', 'APP_HOME', 'APPLICATION_PATH']
        }
        
        relevant_vars = path_env_vars.get(app_type, []) + ['APP_HOME', 'APPLICATION_PATH', 'WORKDIR']
        
        for var_name in relevant_vars:
            value = env_vars.get(var_name, '')
            if value and value.startswith('/') and not value.startswith('/usr/lib'):
                # Clean up the path
                clean_path = value.split(':')[0]  # Handle PATH-like variables
                if os.path.isabs(clean_path):
                    paths.add(clean_path)
        
        return list(paths)
    
    def _verify_source_code(self, path: str, app_type: str) -> bool:
        """Verify that extracted path contains valid source code"""
        
        if not os.path.exists(path):
            return False
        
        # Check for type-specific files
        verification_files = {
            ApplicationType.PYTHON: ['*.py', 'requirements.txt', 'setup.py'],
            ApplicationType.GO: ['*.go', 'go.mod'],
            ApplicationType.JAVASCRIPT: ['*.js', 'package.json'],
            ApplicationType.DOTNET: ['*.cs', '*.csproj', '*.sln']
        }
        
        patterns = verification_files.get(app_type, [])
        
        for pattern in patterns:
            # Use glob to find files
            import glob
            matches = glob.glob(os.path.join(path, '**', pattern), recursive=True)
            if matches:
                logger.info(f"Verified source code: found {len(matches)} {pattern} files")
                return True
        
        return False
    
    def create_gitlab_repository(self, app_name: str, source_path: str) -> Optional[str]:
        """
        Create GitLab repository and push source code
        
        Returns:
            Repository URL or None if failed
        """
        logger.info(f"Creating GitLab repository for: {app_name}")
        
        try:
            # Create repository via GitLab API
            api_url = f"{self.gitlab_url}/api/v4/projects"
            
            project_data = {
                "name": app_name,
                "visibility": "private",
                "initialize_with_readme": False
            }
            
            if self.gitlab_group:
                project_data["namespace_id"] = self._get_gitlab_group_id(self.gitlab_group)
            
            # Create project using curl
            cmd = [
                'curl', '-k', '-X', 'POST',
                '-H', f'PRIVATE-TOKEN: {self.gitlab_token}',
                '-H', 'Content-Type: application/json',
                '-d', json.dumps(project_data),
                api_url
            ]
            
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                timeout=30
            )
            
            if result.returncode != 0:
                logger.error(f"Failed to create GitLab repository: {result.stderr}")
                return None
            
            response = json.loads(result.stdout)
            repo_url = response.get('http_url_to_repo')
            
            if not repo_url:
                logger.error(f"No repository URL in response: {response}")
                return None
            
            logger.info(f"Created GitLab repository: {repo_url}")
            
            # Push code to repository
            if not self._push_to_gitlab(source_path, repo_url, app_name):
                logger.error(f"Failed to push code to GitLab")
                return None
            
            return repo_url
            
        except Exception as e:
            logger.error(f"Error creating GitLab repository: {e}")
            return None
    
    def _get_gitlab_group_id(self, group_path: str) -> Optional[int]:
        """Get GitLab group ID from group path"""
        
        try:
            api_url = f"{self.gitlab_url}/api/v4/groups/{group_path}"
            
            cmd = [
                'curl', '-k', '-X', 'GET',
                '-H', f'PRIVATE-TOKEN: {self.gitlab_token}',
                api_url
            ]
            
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                timeout=10
            )
            
            if result.returncode == 0:
                response = json.loads(result.stdout)
                return response.get('id')
            
        except Exception as e:
            logger.warning(f"Could not get group ID: {e}")
        
        return None
    
    def _push_to_gitlab(self, source_path: str, repo_url: str, app_name: str) -> bool:
        """Push source code to GitLab repository"""
        
        try:
            # Modify repo URL to include token
            url_parts = repo_url.replace('https://', '').replace('http://', '')
            auth_url = f"https://oauth2:{self.gitlab_token}@{url_parts}"
            
            # Initialize git repo
            git_commands = [
                ['git', 'init'],
                ['git', 'config', 'user.email', 'mta-analyzer@example.com'],
                ['git', 'config', 'user.name', 'MTA Analyzer'],
                ['git', 'add', '.'],
                ['git', 'commit', '-m', f'Initial commit for {app_name}'],
                ['git', 'remote', 'add', 'origin', auth_url],
                ['git', 'push', '-u', 'origin', 'master']
            ]
            
            for cmd in git_commands:
                result = subprocess.run(
                    cmd,
                    cwd=source_path,
                    capture_output=True,
                    text=True,
                    encoding='utf-8',
                    errors='replace',
                    timeout=60
                )
                
                if result.returncode != 0 and 'push' in cmd:
                    # Try main branch if master fails
                    cmd[-1] = 'main'
                    result = subprocess.run(
                        cmd,
                        cwd=source_path,
                        capture_output=True,
                        text=True,
                        encoding='utf-8',
                        errors='replace',
                        timeout=60
                    )
                
                if result.returncode != 0:
                    logger.error(f"Git command failed: {' '.join(cmd)}\n{result.stderr}")
                    return False
            
            logger.info(f"Successfully pushed code to GitLab")
            return True
            
        except Exception as e:
            logger.error(f"Error pushing to GitLab: {e}")
            return False
    
    def extract_java_artifact(self, image_url: str, app_name: str, 
                             metadata: Dict[str, Any]) -> Optional[str]:
        """Extract JAR/WAR artifact for Java applications"""
        
        logger.info(f"Extracting Java artifact for: {app_name}")
        
        app_dir = os.path.join(self.output_dir, app_name)
        os.makedirs(app_dir, exist_ok=True)
        
        container_id = None
        try:
            # Create container
            result = subprocess.run(
                ['docker', 'create', image_url],
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                check=True
            )
            container_id = result.stdout.strip()
            
            # Search for JAR/WAR files
            search_paths = ['/opt/app', '/app', '/deployments', '/usr/local/app', '/']
            
            found_artifacts = []
            for search_path in search_paths:
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
                    found_artifacts.extend([f.strip() for f in files if f.strip()])
                    
                    if found_artifacts:
                        break
            
            if not found_artifacts:
                logger.error(f"No JAR/WAR files found")
                return None
            
            # Filter and select best artifact
            app_artifacts = [
                f for f in found_artifacts
                if not any(lib in f.lower() for lib in ['/lib/', '/libs/', 'maven', 'gradle'])
            ]
            
            selected_artifact = app_artifacts[0] if app_artifacts else found_artifacts[0]
            logger.info(f"Selected artifact: {selected_artifact}")
            
            # Copy artifact
            artifact_name = os.path.basename(selected_artifact)
            local_path = os.path.join(app_dir, artifact_name)
            
            result = subprocess.run(
                ['docker', 'cp', f"{container_id}:{selected_artifact}", local_path],
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace'
            )
            
            if result.returncode == 0:
                logger.info(f"Extracted artifact to: {local_path}")
                return local_path
            else:
                logger.error(f"Failed to copy artifact: {result.stderr}")
                return None
            
        except Exception as e:
            logger.error(f"Error extracting Java artifact: {e}")
            return None
            
        finally:
            if container_id:
                subprocess.run(
                    ['docker', 'rm', '-f', container_id],
                    capture_output=True,
                    stderr=subprocess.DEVNULL
                )
    
    def curl_request(self, method: str, url: str, data: Optional[str] = None,
                    file_path: Optional[str] = None, file_field_name: str = 'file') -> Tuple[int, str]:
        """Make HTTP request using curl"""
        
        cmd = ['curl', '-k', '-X', method, '-w', '\\n%{http_code}']
        
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
                timeout=300
            )
            
            output = result.stdout
            lines = output.strip().split('\n')
            response_body = '\n'.join(lines[:-1]) if len(lines) > 1 else ''
            status_code = int(lines[-1]) if lines[-1].isdigit() else 0
            
            return status_code, response_body
            
        except Exception as e:
            logger.error(f"Curl request failed: {e}")
            return 0, str(e)
    
    def create_mta_application(self, app_name: str) -> Optional[int]:
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
                logger.error(f"Failed to parse response: {response}")
                return None
        else:
            logger.error(f"Failed to create application. Status: {status_code}")
            return None
    
    def create_mta_task_binary(self, app_id: int, artifact_name: str) -> Optional[int]:
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
                logger.error(f"Failed to parse response: {response}")
                return None
        else:
            logger.error(f"Failed to create task. Status: {status_code}")
            return None
    
    def create_mta_task_source(self, app_id: int, repo_url: str, branch: str = 'master') -> Optional[int]:
        """Create source code analysis task in MTA"""
        
        logger.info(f"Creating source code analysis task for app ID: {app_id}")
        
        url = f"{self.mta_url}/hub/tasks"
        
        task_data = {
            "name": f"{app_id}-Analyse-Source",
            "application": {"id": app_id},
            "kind": "analyzer",
            "addon": "analyzer",
            "data": {
                "mode": {
                    "binary": False,
                    "withDeps": False
                },
                "targets": ["cloud-readiness"],
                "sources": [],
                "scope": {
                    "withKnownLibs": False
                },
                "rules": {
                    "path": "",
                    "tags": {
                        "excluded": []
                    }
                }
            }
        }
        
        data = json.dumps(task_data)
        status_code, response = self.curl_request('POST', url, data=data)
        
        if status_code in [200, 201]:
            try:
                task_response = json.loads(response)
                task_id = task_response.get('id')
                logger.info(f"Created source task with ID: {task_id}")
                
                # Attach repository to task
                if not self._attach_repository_to_task(task_id, repo_url, branch):
                    logger.error(f"Failed to attach repository to task")
                    return None
                
                return task_id
            except json.JSONDecodeError:
                logger.error(f"Failed to parse response: {response}")
                return None
        else:
            logger.error(f"Failed to create task. Status: {status_code}")
            return None
    
    def _attach_repository_to_task(self, task_id: int, repo_url: str, branch: str) -> bool:
        """Attach Git repository to MTA task"""
        
        logger.info(f"Attaching repository to task {task_id}")
        
        url = f"{self.mta_url}/hub/tasks/{task_id}"
        
        # Get current task data
        status_code, response = self.curl_request('GET', url)
        
        if status_code != 200:
            logger.error(f"Failed to get task data")
            return False
        
        try:
            task_data = json.loads(response)
            
            # Update with repository information
            if 'data' not in task_data:
                task_data['data'] = {}
            
            task_data['data']['repository'] = {
                "url": repo_url,
                "branch": branch,
                "path": "/"
            }
            
            # Update task
            data = json.dumps(task_data)
            status_code, response = self.curl_request('PUT', url, data=data)
            
            if status_code in [200, 204]:
                logger.info(f"Successfully attached repository to task")
                return True
            else:
                logger.error(f"Failed to update task. Status: {status_code}")
                return False
                
        except Exception as e:
            logger.error(f"Error attaching repository: {e}")
            return False
    
    def upload_artifact(self, task_id: int, artifact_path: str) -> bool:
        """Upload binary artifact to MTA task"""
        
        artifact_name = os.path.basename(artifact_path)
        logger.info(f"Uploading artifact: {artifact_name}")
        
        url = f"{self.mta_url}/hub/tasks/{task_id}/bucket/binary/{artifact_name}"
        
        file_size = os.path.getsize(artifact_path)
        file_size_mb = file_size / (1024 * 1024)
        logger.info(f"Artifact size: {file_size_mb:.2f} MB")
        
        status_code, response = self.curl_request('PUT', url, file_path=artifact_path, file_field_name='file')
        
        if status_code in [200, 201, 204]:
            logger.info(f"Successfully uploaded artifact")
            return True
        else:
            logger.error(f"Failed to upload artifact. Status: {status_code}")
            return False
    
    def submit_task(self, task_id: int) -> bool:
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
        """
        Process application through complete workflow with detailed reporting
        Robust error handling ensures one failure doesn't stop the entire process
        """
        
        logger.info(f"\n{'='*60}")
        logger.info(f"Processing application: {app_name}")
        logger.info(f"Image: {image_url}")
        logger.info(f"{'='*60}\n")
        
        success = False
        app_type = ApplicationType.UNKNOWN
        metadata = {}
        report_path = ""
        
        try:
            # Step 1: Pull Docker image (with error handling)
            try:
                if not self.pull_docker_image(image_url):
                    logger.error(f"Failed to pull image for {app_name}")
                    # Export detection report even on failure
                    try:
                        self.export_detection_report(app_name, image_url, ApplicationType.UNKNOWN,
                                                    {'detection_method': 'failed_image_pull',
                                                     'detection_details': ['Image pull failed'],
                                                     'fallback_attempts': ['image_pull_failed']})
                    except Exception as report_err:
                        logger.warning(f"Could not export failure report: {report_err}")
                    return False
            except Exception as pull_err:
                logger.error(f"Exception during image pull: {pull_err}")
                return False
            
            # Step 2: Detect application type (with error handling)
            try:
                app_type, metadata = self.detect_application_type(image_url)
                logger.info(f"Application type: {app_type}")
            except Exception as detect_err:
                logger.error(f"Exception during type detection: {detect_err}")
                app_type = ApplicationType.UNKNOWN
                metadata = {
                    'detection_method': 'exception_during_detection',
                    'detection_details': [f'Detection error: {str(detect_err)}'],
                    'fallback_attempts': ['detection_exception']
                }
            
            # Step 2.5: Export detection report (always, with error handling)
            try:
                report_path = self.export_detection_report(app_name, image_url, app_type, metadata)
                if report_path:
                    logger.info(f"Detection report saved: {report_path}")
            except Exception as report_err:
                logger.warning(f"Could not export detection report: {report_err}")
                report_path = "report_export_failed"
            
            # Step 3: Create MTA application (with error handling)
            app_id = None
            try:
                app_id = self.create_mta_application(app_name)
                if not app_id:
                    logger.error(f"Failed to create MTA application")
                    return False
            except Exception as mta_err:
                logger.error(f"Exception creating MTA application: {mta_err}")
                return False
            
            # Step 4: Process based on application type (with error handling)
            try:
                if app_type == ApplicationType.JAVA:
                    success = self._process_java_application(app_id, app_name, image_url, metadata)
                elif app_type != ApplicationType.UNKNOWN:
                    success = self._process_source_application(app_id, app_name, image_url, app_type, metadata)
                else:
                    logger.warning(f"Unknown application type for {app_name}. Skipping MTA analysis.")
                    if report_path:
                        logger.warning(f"Check detection report for details: {report_path}")
                    success = False
            except Exception as process_err:
                logger.error(f"Exception during application processing: {process_err}")
                success = False
            
            if success:
                logger.info(f"\n✓ Successfully processed {app_name}")
                logger.info(f"  Application ID: {app_id}")
                logger.info(f"  Type: {app_type}")
                if report_path:
                    logger.info(f"  Detection Report: {report_path}\n")
            else:
                logger.warning(f"\n✗ Failed to process {app_name}")
                if report_path:
                    logger.warning(f"  Detection Report: {report_path}\n")
            
            return success
            
        except Exception as e:
            logger.error(f"Unexpected error processing application {app_name}: {e}")
            logger.error(f"Error type: {type(e).__name__}")
            
            # Try to export error report (best effort)
            try:
                error_metadata = metadata.copy() if metadata else {}
                error_metadata['detection_details'] = error_metadata.get('detection_details', []) + [
                    f"Unexpected error: {str(e)}",
                    f"Error type: {type(e).__name__}"
                ]
                error_metadata['fallback_attempts'] = error_metadata.get('fallback_attempts', []) + ['unexpected_exception']
                self.export_detection_report(app_name, image_url, app_type, error_metadata)
            except Exception as final_err:
                logger.warning(f"Could not export error report: {final_err}")
            
            return False
        
        finally:
            # Cleanup (with error handling)
            if self.cleanup:
                try:
                    self._cleanup_application(app_name, image_url, success)
                except Exception as cleanup_err:
                    logger.warning(f"Error during cleanup for {app_name}: {cleanup_err}")
    
    def _process_java_application(self, app_id: int, app_name: str, 
                                  image_url: str, metadata: Dict[str, Any]) -> bool:
        """Process Java application with binary analysis"""
        
        logger.info(f"Processing as Java application (binary analysis)")
        
        # Extract JAR/WAR artifact
        artifact_path = self.extract_java_artifact(image_url, app_name, metadata)
        if not artifact_path:
            logger.error(f"Failed to extract Java artifact")
            return False
        
        artifact_name = os.path.basename(artifact_path)
        
        # Create binary analysis task
        task_id = self.create_mta_task_binary(app_id, artifact_name)
        if not task_id:
            logger.error(f"Failed to create binary task")
            return False
        
        # Upload artifact
        if not self.upload_artifact(task_id, artifact_path):
            logger.error(f"Failed to upload artifact")
            return False
        
        # Submit task
        if not self.submit_task(task_id):
            logger.error(f"Failed to submit task")
            return False
        
        logger.info(f"  Task ID: {task_id}")
        logger.info(f"  Artifact: {artifact_name}")
        
        return True
    
    def _process_source_application(self, app_id: int, app_name: str, image_url: str,
                                    app_type: str, metadata: Dict[str, Any]) -> bool:
        """Process non-Java application with source code analysis"""
        
        logger.info(f"Processing as {app_type} application (source code analysis)")
        
        # Extract source code
        source_path = self.extract_source_code(image_url, app_name, app_type, metadata)
        if not source_path:
            logger.error(f"Failed to extract source code")
            return False
        
        # Create GitLab repository
        repo_url = self.create_gitlab_repository(app_name, source_path)
        if not repo_url:
            logger.error(f"Failed to create GitLab repository")
            return False
        
        # Create source code analysis task
        task_id = self.create_mta_task_source(app_id, repo_url)
        if not task_id:
            logger.error(f"Failed to create source task")
            return False
        
        # Submit task
        if not self.submit_task(task_id):
            logger.error(f"Failed to submit task")
            return False
        
        logger.info(f"  Task ID: {task_id}")
        logger.info(f"  Repository: {repo_url}")
        
        return True
    
    def _cleanup_application(self, app_name: str, image_url: str, success: bool):
        """Cleanup Docker images and artifacts"""
        
        logger.info(f"Performing cleanup for {app_name}...")
        
        # Cleanup Docker image
        try:
            subprocess.run(
                ['docker', 'rmi', '-f', image_url],
                capture_output=True,
                stderr=subprocess.DEVNULL,
                timeout=30
            )
        except:
            pass
        
        # Cleanup artifacts only if successful
        if success:
            app_dir = os.path.join(self.output_dir, app_name)
            if os.path.exists(app_dir):
                try:
                    shutil.rmtree(app_dir)
                    logger.info(f"Cleaned up artifacts for {app_name}")
                except Exception as e:
                    logger.warning(f"Failed to cleanup artifacts: {e}")
    
    def process_all(self, csv_path: str) -> Dict[str, List[str]]:
        """
        Process all applications from CSV with comprehensive tracking
        Generates master summary CSV for easy analysis of 1200+ images
        """
        
        results = {'successful': [], 'failed': []}
        detailed_results = []  # Track detailed status for each application
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
                image_url = app['image_url']
                
                result_entry = {
                    'app_name': app_name,
                    'image_url': image_url,
                    'status': 'unknown',
                    'error_type': '',
                    'error_message': '',
                    'detected_type': '',
                    'detection_method': '',
                    'confidence_score': 0,
                    'mta_app_id': '',
                    'mta_task_id': '',
                    'report_path': '',
                    'processing_time': 0,
                    'timestamp': datetime.now().isoformat()
                }
                
                process_start = time.time()
                
                try:
                    success = future.result()
                    result_entry['processing_time'] = time.time() - process_start
                    
                    if success:
                        result_entry['status'] = 'success'
                        results['successful'].append(app_name)
                    else:
                        result_entry['status'] = 'failed'
                        results['failed'].append(app_name)
                        
                except Exception as e:
                    result_entry['processing_time'] = time.time() - process_start
                    result_entry['status'] = 'exception'
                    result_entry['error_type'] = type(e).__name__
                    result_entry['error_message'] = str(e)[:200]  # Limit error message length
                    logger.error(f"Exception processing {app_name}: {e}")
                    results['failed'].append(app_name)
                
                # Try to get additional details from individual report
                try:
                    safe_name = "".join(c if c.isalnum() or c in ('-', '_') else '_' for c in app_name)
                    report_path = os.path.join('reports', f"{safe_name}_detection_report.csv")
                    if os.path.exists(report_path):
                        result_entry['report_path'] = report_path
                        # Try to extract key info from report
                        with open(report_path, 'r', encoding='utf-8', errors='replace') as f:
                            for line in f:
                                if 'Detected Type' in line:
                                    parts = line.strip().split(',')
                                    if len(parts) >= 3:
                                        result_entry['detected_type'] = parts[2]
                                elif 'Detection Method' in line:
                                    parts = line.strip().split(',')
                                    if len(parts) >= 3:
                                        result_entry['detection_method'] = parts[2]
                                elif 'Confidence Score' in line:
                                    parts = line.strip().split(',')
                                    if len(parts) >= 3:
                                        try:
                                            result_entry['confidence_score'] = int(parts[2])
                                        except:
                                            pass
                except Exception as report_err:
                    logger.debug(f"Could not extract details from report for {app_name}: {report_err}")
                
                detailed_results.append(result_entry)
        
        elapsed_time = (datetime.now() - start_time).total_seconds()
        
        # Export master summary CSV
        summary_csv_path = self._export_master_summary(detailed_results, elapsed_time)
        
        # Print summary
        logger.info(f"\n{'='*60}")
        logger.info(f"PROCESSING SUMMARY")
        logger.info(f"{'='*60}")
        logger.info(f"Total applications: {len(applications)}")
        logger.info(f"Successful: {len(results['successful'])}")
        logger.info(f"Failed: {len(results['failed'])}")
        logger.info(f"Total time: {elapsed_time:.2f} seconds")
        logger.info(f"Average time per app: {elapsed_time/len(applications):.2f} seconds")
        logger.info(f"\nMaster summary CSV: {summary_csv_path}")
        
        if results['failed']:
            logger.info(f"\nFailed applications ({len(results['failed'])}):")
            for app in results['failed'][:20]:  # Show first 20
                logger.info(f"  - {app}")
            if len(results['failed']) > 20:
                logger.info(f"  ... and {len(results['failed']) - 20} more (see {summary_csv_path})")
        
        return results
    
    def _export_master_summary(self, detailed_results: List[Dict[str, Any]], elapsed_time: float) -> str:
        """
        Export master summary CSV with all applications and their status
        Perfect for analyzing 1200+ images
        """
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        summary_path = f"mta_analysis_summary_{timestamp}.csv"
        
        try:
            with open(summary_path, 'w', newline='', encoding='utf-8', errors='replace') as csvfile:
                writer = csv.writer(csvfile)
                
                # Header
                writer.writerow([
                    'App Name',
                    'Image URL',
                    'Status',
                    'Error Type',
                    'Error Message',
                    'Detected Type',
                    'Detection Method',
                    'Confidence Score',
                    'MTA App ID',
                    'MTA Task ID',
                    'Report Path',
                    'Processing Time (s)',
                    'Timestamp'
                ])
                
                # Sort by status (failed first for easy review)
                sorted_results = sorted(detailed_results, key=lambda x: (
                    0 if x['status'] == 'exception' else 1 if x['status'] == 'failed' else 2,
                    x['app_name']
                ))
                
                # Data rows
                for result in sorted_results:
                    writer.writerow([
                        result.get('app_name', ''),
                        result.get('image_url', ''),
                        result.get('status', ''),
                        result.get('error_type', ''),
                        result.get('error_message', ''),
                        result.get('detected_type', ''),
                        result.get('detection_method', ''),
                        result.get('confidence_score', 0),
                        result.get('mta_app_id', ''),
                        result.get('mta_task_id', ''),
                        result.get('report_path', ''),
                        f"{result.get('processing_time', 0):.2f}",
                        result.get('timestamp', '')
                    ])
                
                # Summary statistics at the end
                writer.writerow([])
                writer.writerow(['SUMMARY STATISTICS'])
                writer.writerow(['Total Applications', len(detailed_results)])
                writer.writerow(['Successful', len([r for r in detailed_results if r['status'] == 'success'])])
                writer.writerow(['Failed', len([r for r in detailed_results if r['status'] == 'failed'])])
                writer.writerow(['Exceptions', len([r for r in detailed_results if r['status'] == 'exception'])])
                writer.writerow(['Total Processing Time (s)', f"{elapsed_time:.2f}"])
                writer.writerow(['Average Time per App (s)', f"{elapsed_time/len(detailed_results):.2f}" if detailed_results else "0"])
                
                # Breakdown by detected type
                writer.writerow([])
                writer.writerow(['BREAKDOWN BY TYPE'])
                type_counts = {}
                for result in detailed_results:
                    app_type = result.get('detected_type', 'unknown')
                    type_counts[app_type] = type_counts.get(app_type, 0) + 1
                
                for app_type, count in sorted(type_counts.items(), key=lambda x: x[1], reverse=True):
                    writer.writerow([app_type, count])
                
                # Breakdown by error type
                writer.writerow([])
                writer.writerow(['BREAKDOWN BY ERROR TYPE'])
                error_counts = {}
                for result in detailed_results:
                    if result['status'] in ['failed', 'exception']:
                        error_type = result.get('error_type', 'unknown_error')
                        if not error_type:
                            error_type = 'processing_failed'
                        error_counts[error_type] = error_counts.get(error_type, 0) + 1
                
                for error_type, count in sorted(error_counts.items(), key=lambda x: x[1], reverse=True):
                    writer.writerow([error_type, count])
            
            logger.info(f"Master summary exported to: {summary_path}")
            return summary_path
            
        except Exception as e:
            logger.error(f"Failed to export master summary: {e}")
            return "summary_export_failed"


def parse_arguments():
    """Parse command line arguments"""
    
    parser = argparse.ArgumentParser(
        description='MTA Multi-Language Analyzer - Supports Java, Python, Go, JavaScript, and .NET'
    )
    
    parser.add_argument(
        'input_csv',
        help='Path to input CSV file with app_name and image_url columns'
    )
    
    parser.add_argument(
        '--mta-url',
        help='MTA base URL (e.g., https://mta.example.com) - Required unless --discovery-only',
        default=''
    )
    
    parser.add_argument(
        '--gitlab-url',
        help='GitLab instance URL (e.g., https://gitlab.example.com) - Required unless --discovery-only',
        default=''
    )
    
    parser.add_argument(
        '--gitlab-token',
        help='GitLab personal access token - Required unless --discovery-only',
        default=''
    )
    
    parser.add_argument(
        '--gitlab-group',
        help='GitLab group/namespace for repositories (optional)',
        default=None
    )
    
    parser.add_argument(
        '--output-dir',
        help='Directory for extracted artifacts (default: artifacts)',
        default='artifacts'
    )
    
    parser.add_argument(
        '--no-cleanup',
        action='store_true',
        help='Keep Docker images and artifacts after processing'
    )
    
    parser.add_argument(
        '--max-workers',
        type=int,
        default=4,
        help='Maximum number of concurrent workers (default: 4)'
    )
    
    parser.add_argument(
        '--discovery-only',
        action='store_true',
        help='Discovery mode: Only gather information without MTA processing (no GitLab/MTA required)'
    )
    
    return parser.parse_args()


def main():
    """Main entry point"""
    
    args = parse_arguments()
    
    # Validate required arguments for full analysis mode
    if not args.discovery_only:
        if not args.mta_url or not args.gitlab_url or not args.gitlab_token:
            logger.error("ERROR: --mta-url, --gitlab-url, and --gitlab-token are required for full analysis mode")
            logger.error("Use --discovery-only flag to run without MTA/GitLab integration")
            sys.exit(1)
    
    logger.info("="*60)
    logger.info("MTA Multi-Language Analyzer")
    logger.info("="*60)
    logger.info(f"Mode: {'DISCOVERY ONLY' if args.discovery_only else 'FULL ANALYSIS'}")
    logger.info(f"MTA URL: {args.mta_url if not args.discovery_only else 'N/A (discovery mode)'}")
    logger.info(f"GitLab URL: {args.gitlab_url if not args.discovery_only else 'N/A (discovery mode)'}")
    logger.info(f"Input CSV: {args.input_csv}")
    logger.info(f"Output Directory: {args.output_dir}")
    logger.info(f"Cleanup: {not args.no_cleanup}")
    logger.info(f"Max Workers: {args.max_workers}")
    logger.info("="*60 + "\n")
    
    cleanup_enabled = not args.no_cleanup
    
    # In discovery mode, MTA and GitLab are optional
    mta_url = args.mta_url if not args.discovery_only else ''
    gitlab_url = args.gitlab_url if not args.discovery_only else ''
    gitlab_token = args.gitlab_token if not args.discovery_only else ''
    
    try:
        analyzer = MTAMultiLanguageAnalyzer(
            mta_url=mta_url,
            gitlab_url=gitlab_url,
            gitlab_token=gitlab_token,
            gitlab_group=args.gitlab_group,
            output_dir=args.output_dir,
            cleanup=cleanup_enabled,
            max_workers=args.max_workers,
            discovery_only=args.discovery_only
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

# Made with Bob
