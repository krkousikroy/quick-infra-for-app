# ---- Build Stage ----
FROM docker.io/maven:3.9-eclipse-temurin-21-alpine AS builder
WORKDIR /build
COPY pom.xml .
COPY src ./src
# Use Maven Wrapper if present, else fallback to system mvn
RUN [ -f ./mvnw ] && ./mvnw -B package -DskipTests || mvn -B package -DskipTests

# ---- Run Stage ----
FROM docker.io/eclipse-temurin:21-jre-alpine
WORKDIR /app
# Copy the generated jar (assumes only one jar in target/ ending with .jar)
COPY --from=builder /build/target/*.jar app.jar
EXPOSE 8080
ENTRYPOINT ["java", "-jar", "app.jar"]
