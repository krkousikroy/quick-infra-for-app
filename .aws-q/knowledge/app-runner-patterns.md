# AWS App Runner Deployment Patterns
# Knowledge base for AWS Q Developer agentic decision making

## Optimal Configuration Patterns

### CPU and Memory Sizing
```yaml
Micro Services (< 100 RPS):
  cpu: 256-512
  memory: 512-1024

Standard Applications (100-1000 RPS):
  cpu: 1024
  memory: 2048

High Traffic Applications (> 1000 RPS):
  cpu: 2048-4096
  memory: 4096-8192
```

### Auto-scaling Configurations
```yaml
Conservative Scaling:
  min_instances: 1
  max_instances: 5
  target_utilization: 70%

Aggressive Scaling:
  min_instances: 2
  max_instances: 25
  target_utilization: 50%

Cost-Optimized:
  min_instances: 1
  max_instances: 10
  target_utilization: 80%
```

### VPC Integration Best Practices
- Always use VPC connectors for database access
- Place App Runner in private subnets
- Use VPC endpoints for AWS service communication
- Implement security groups with least privilege
- Enable VPC Flow Logs for network monitoring

### Database Connection Patterns
```java
// Optimal connection pool configuration
spring.datasource.hikari.maximum-pool-size=10
spring.datasource.hikari.minimum-idle=2
spring.datasource.hikari.connection-timeout=20000
spring.datasource.hikari.idle-timeout=300000
spring.datasource.hikari.max-lifetime=1200000
```

### Security Hardening Checklist
- [ ] Customer-managed KMS encryption
- [ ] Secrets Manager for credentials
- [ ] VPC-only network access
- [ ] IAM roles with least privilege
- [ ] Container image vulnerability scanning
- [ ] Network security groups properly configured
- [ ] CloudTrail logging enabled
- [ ] GuardDuty threat detection active

### Performance Optimization
- Use multi-stage Docker builds
- Implement application health checks
- Configure proper logging levels
- Use connection pooling for databases
- Implement caching strategies
- Monitor with CloudWatch and X-Ray

### Cost Optimization Strategies
- Right-size CPU and memory allocations
- Use predictive scaling policies
- Implement proper auto-scaling thresholds
- Monitor and optimize container image sizes
- Use reserved capacity for predictable workloads
- Implement proper resource tagging for cost allocation