# Generic Application Infrastructure

This repository contains CloudFormation templates for deploying a complete application infrastructure on AWS, including base infrastructure components and application-specific resources.

## Architecture Overview

The infrastructure is split into two main stacks:

1. **Base Infrastructure Stack** (`base-infrastructure-stack.yaml`) - Contains shared resources
2. **Application Stack** (`application-service-stack.yaml`) - Contains application-specific resources

## Base Infrastructure Components

- **VPC** with public, private, and database subnets across 2 AZs
- **NAT Gateways** for private subnet internet access
- **RDS PostgreSQL** database with encryption and backup
- **KMS Key** for encryption across all services
- **Security Groups** for database and application access
- **VPC Endpoints** for App Runner private access
- **App Runner VPC Connector** for secure VPC connectivity
- **Secrets Manager** for database credentials and application secrets
- **CodeStar Connection** for GitHub integration
- **S3 Bucket** for pipeline artifacts (shared across services)
- **Lambda Function** for pipeline completion synchronization

## Application Infrastructure Components

- **ECR Repository** for container images
- **CodeBuild Project** for building Docker images
- **CodePipeline** for CI/CD automation
- **App Runner Service** with VPC connectivity for secure container hosting

## Deployment Instructions

### Prerequisites

1. AWS CLI configured with appropriate permissions
2. GitHub repository with your application code
3. Dockerfile in your repository root

## Sample Spring Boot Application

This repository includes a sample Spring Boot application for testing the infrastructure:

### Application Structure
```
src/
└── main/
    ├── java/com/example/demo/
    │   └── DemoApplication.java
    └── resources/
        └── application.properties
pom.xml
Dockerfile
```

### Endpoints
- `GET /health` - Health check endpoint
- `GET /dbtest` - Database connectivity test

### Features
- **Database Integration**: Connects to PostgreSQL using environment variables from Secrets Manager
- **Health Monitoring**: Basic health check endpoint for App Runner
- **Multi-stage Docker Build**: Optimized Dockerfile with build and runtime stages
- **Environment Configuration**: Uses Spring Boot profiles with environment variables

### Environment Variables (Auto-configured)
The application uses these environment variables provided by the infrastructure:
- `DB_URL` - Complete PostgreSQL connection string
- `DB_USER` - Database username
- `DB_PASSWORD` - Database password
- `PORT` - Application port (defaults to 8080)

### Step 1: Deploy Base Infrastructure

1. Update the base infrastructure parameters:
```bash
cp base-infrastructure-parameters.json base-infrastructure-parameters-prod.json
# Edit the parameters file with your values
```

2. Deploy the base infrastructure stack:
```bash
aws cloudformation deploy \
  --template-file base-infrastructure-stack.yaml \
  --stack-name bookworm-base-infrastructure \
  --parameter-overrides file://base-infrastructure-parameters.json \
  --capabilities CAPABILITY_NAMED_IAM \
  --region us-east-1 \
  --tags Project=bookworm Environment=prod Owner=DPDE
```

3. **Important**: After deployment, activate the GitHub connection:
   - Go to AWS Console → Developer Tools → Settings → Connections
   - Find your connection and click "Update pending connection"
   - Complete the GitHub authorization process

### Step 2: Deploy Application Infrastructure

1. Update the application parameters:
```bash
cp application-parameters-template.json application-parameters.json
# Edit the parameters file with your values
```

2. Deploy the application stack:
```bash
aws cloudformation deploy \
  --template-file application-service-stack.yaml \
  --stack-name bookworm-catalog-service \
  --parameter-overrides file://application-parameters.json \
  --capabilities CAPABILITY_NAMED_IAM \
  --region us-east-1 \
  --tags Project=bookworm Environment=prod Owner=DPDE Service=catalog-service
```

### Step 3: Verify Deployment

1. Check stack outputs:
```bash
aws cloudformation describe-stacks --stack-name bookworm-base-infrastructure --region us-east-1 --query 'Stacks[0].Outputs'
aws cloudformation describe-stacks --stack-name bookworm-catalog-service --region us-east-1 --query 'Stacks[0].Outputs'
```

2. The pipeline will automatically trigger on the first deployment and build your application.

3. Access your application via the VPC endpoint. The App Runner service is configured for private access through the VPC endpoint created in the base infrastructure.

### Testing the Sample Application

Once deployed, you can test the sample Spring Boot application:

1. **Health Check**:
   ```bash
   curl https://<app-runner-url>/health
   # Expected: OK
   ```

2. **Database Connectivity**:
   ```bash
   curl https://<app-runner-url>/dbtest
   # Expected: DB Connection: SUCCESS
   ```

## Configuration Parameters

### Base Infrastructure Parameters

| Parameter | Description | Default |
|-----------|-------------|---------|
| ProjectName | Project name for resource naming | - |
| Environment | Environment (dev/staging/prod) | prod |
| Owner | Team or owner name | - |
| VpcCidr | VPC CIDR block | 10.0.0.0/20 |
| DatabaseName | Database name | appdb |
| DatabaseUsername | Database master username | dbadmin |
| DatabaseInstanceClass | RDS instance class | db.t3.small |
| DatabaseAllocatedStorage | Database storage in GB | 20 |
| EnableMultiAZ | Enable Multi-AZ for RDS | false |
| Region | AWS Region for deployment | us-east-1 |

### Application Parameters

| Parameter | Description | Default |
|-----------|-------------|---------|
| ServiceName | Name of the service/application | - |
| GitHubRepo | GitHub repository (owner/repo) | - |
| BaseInfrastructureStackName | Base infrastructure stack name | - |
| ContainerPort | Application container port | 8080 |
| AppRunnerCpu | CPU units (256-4096) | 1024 |
| AppRunnerMemory | Memory in MB | 2048 |
| CreateSecretsConfig | Configure database secrets | true |
| Region | AWS Region for deployment | us-east-1 |

## Environment Variables

When `CreateSecretsConfig` is enabled, the following environment variables are available to your application:

- `DB_URL` - Complete database connection string
- `DB_USER` - Database username
- `DB_PASSWORD` - Database password
- `DB_HOST` - Database host
- `DB_PORT` - Database port
- `DB_NAME` - Database name

## Security Features

- All data encrypted at rest using KMS
- App Runner VPC connectivity for private database access
- App Runner ingress through VPC endpoint (not publicly accessible)
- Security groups with least privilege access
- Secrets Manager for credential management
- IAM roles with minimal required permissions
- Private subnets for database resources and App Runner VPC connectivity

## Monitoring and Logging

- CloudWatch Logs for all services
- RDS Enhanced Monitoring (production only)
- ECR image scanning enabled
- Pipeline execution logs in CloudWatch

## Cleanup

To delete the infrastructure:

1. Delete application stack first:
```bash
aws cloudformation delete-stack --stack-name bookworm-catalog-service --region us-east-1
```

2. Wait for completion, then delete base infrastructure:
```bash
aws cloudformation delete-stack --stack-name bookworm-base-infrastructure --region us-east-1
```

**Note**: Ensure all ECR images are deleted before stack deletion to avoid errors.

## Customization

### Adding New Applications

1. Copy `application-service-stack.yaml` and rename it
2. Update the parameters file with new service details
3. Deploy using the same process

### Modifying Base Infrastructure

Update `base-infrastructure-stack.yaml` and redeploy. Application stacks will automatically use the updated resources via CloudFormation exports.

## Troubleshooting

### Common Issues

1. **GitHub Connection Pending**: Manually activate the connection in AWS Console
2. **Pipeline Fails**: Check CodeBuild logs in CloudWatch
3. **App Runner Deployment Fails**: Verify Dockerfile and container port configuration
4. **Database Connection Issues**: Check security group rules and VPC configuration

### Useful Commands

```bash
# Check stack events
aws cloudformation describe-stack-events --stack-name <stack-name> --region us-east-1

# View CodeBuild logs
aws logs describe-log-groups --log-group-name-prefix /aws/codebuild/ --region us-east-1

# Check App Runner service status
aws apprunner describe-service --service-arn <service-arn> --region us-east-1
```