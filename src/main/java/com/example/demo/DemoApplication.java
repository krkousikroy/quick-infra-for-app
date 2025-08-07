package com.example.demo;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.beans.factory.annotation.Value;
import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.SQLException;

@SpringBootApplication
@RestController
public class DemoApplication {

    @Value("${spring.datasource.url}")
    private String dbUrl;

    @Value("${spring.datasource.username}")
    private String dbUser;

    @Value("${spring.datasource.password}")
    private String dbPassword;

    public static void main(String[] args) {
        SpringApplication.run(DemoApplication.class, args);
    }

    @GetMapping("/health")
    public String health() {
        return "OK";
    }

    @GetMapping("/dbtest")
    public String dbTest() {
        try (Connection conn = DriverManager.getConnection(dbUrl, dbUser, dbPassword)) {
            if (conn != null && !conn.isClosed()) {
                return "DB Connection: SUCCESS";
            } else {
                return "DB Connection: FAILED";
            }
        } catch (SQLException e) {
            return "DB Connection: ERROR - " + e.getMessage();
        }
    }
}
