@echo off
setlocal EnableDelayedExpansion

:: ============================================================
::  MTA 8.0.1 Windows Automation Script
::  Uses: docker, curl, tar (built-in to Windows 10+)
::  Reads: input.csv  (app_name,image_url)
::  Steps:
::    1. Pull Docker image
::    2. Extract JAR/WAR/EAR binaries
::    3. Create MTA Application via curl (HTTPS insecure)
::    4. Upload binary to application bucket via curl
::    5. Create Analysis Task via curl
:: ============================================================

:: -------  USER CONFIGURATION  -------
set "MTA_URL=https://mta-ui.apps.your-cluster.com"
set "MTA_TOKEN="
set "INPUT_CSV=input.csv"
set "TARGETS=cloud-readiness"
set "WORK_DIR=%~dp0mta_workspace"
set "LOG_FILE=%~dp0mta_workflow.log"
:: Skip OpenShift oc extraction (set to 1 if no OC access)
set "SKIP_OC=1"
:: Set to 1 to keep extracted artifacts after upload
set "KEEP_ARTIFACTS=0"
:: ----------------------------------------

:: ---- Parse optional overrides from command line ----
:: Usage: mta_workflow.bat [MTA_URL] [TOKEN] [CSV_FILE] [TARGETS]
if not "%~1"=="" set "MTA_URL=%~1"
if not "%~2"=="" set "MTA_TOKEN=%~2"
if not "%~3"=="" set "INPUT_CSV=%~3"
if not "%~4"=="" set "TARGETS=%~4"

:: Strip trailing slash from URL
if "!MTA_URL:~-1!"=="/" set "MTA_URL=!MTA_URL:~0,-1!"

:: ---- Ignore dir substrings — must NOT contain these paths ----
:: (Checked via findstr logic in binary scan loop)
:: Patterns: WEB-INF/lib  BOOT-INF/lib  /usr/share  /tomcat/lib  etc.
set "IGNORE_PATTERNS=WEB-INF/lib BOOT-INF/lib usr/lib/jvm usr/java jre/lib jdk/lib .m2/ .gradle/ var/cache/ usr/share/ usr/lib64/ modules/system/ wlp/lib/ tomcat/lib/ apache-tomcat/lib/ node_modules/ /libs/ /lib/"

echo.>> "!LOG_FILE!"
echo ============================================================>> "!LOG_FILE!"
echo [MTA-WORKFLOW] Started: %date% %time%>> "!LOG_FILE!"
echo MTA URL : !MTA_URL!>> "!LOG_FILE!"
echo CSV     : !INPUT_CSV!>> "!LOG_FILE!"
echo TARGETS : !TARGETS!>> "!LOG_FILE!"
echo ============================================================>> "!LOG_FILE!"

echo.
echo  =========================================================
echo   Red Hat MTA 8.0.1 - Windows Automation Script
echo  =========================================================
echo   MTA URL  : !MTA_URL!
echo   CSV File : !INPUT_CSV!
echo   Targets  : !TARGETS!
echo   Work Dir : !WORK_DIR!
echo  =========================================================
echo.

:: ---- Pre-flight checks ----
call :log "Running pre-flight checks..."

where docker >nul 2>&1
if errorlevel 1 (
    call :log "ERROR: 'docker' not found in PATH. Please install Docker Desktop."
    exit /b 1
)
call :log "docker ... OK"

where curl >nul 2>&1
if errorlevel 1 (
    call :log "ERROR: 'curl' not found in PATH. Windows 10 1803+ ships with curl."
    exit /b 1
)
call :log "curl   ... OK"

docker info >nul 2>&1
if errorlevel 1 (
    call :log "WARNING: Docker daemon not responding. Ensure Docker Desktop is running."
) else (
    call :log "Docker daemon ... running"
)

if not exist "!INPUT_CSV!" (
    call :log "ERROR: Input CSV '!INPUT_CSV!' not found."
    exit /b 1
)

:: ---- Test MTA API connectivity ----
call :log "Testing MTA API connection..."
if defined MTA_TOKEN (
    curl -sk -o nul -w "%%{http_code}" --max-time 10 -H "Authorization: Bearer !MTA_TOKEN!" "!MTA_URL!/hub/applications" > "%TEMP%\mta_ping.tmp" 2>&1
) else (
    curl -sk -o nul -w "%%{http_code}" --max-time 10 "!MTA_URL!/hub/applications" > "%TEMP%\mta_ping.tmp" 2>&1
)
set /p MTA_PING=<"%TEMP%\mta_ping.tmp"
if "!MTA_PING!"=="200" (
    call :log "MTA API connection successful (HTTP 200)."
) else (
    call :log "WARNING: MTA API returned HTTP !MTA_PING!. Proceeding anyway..."
)

:: ---- Create workspace ----
if not exist "!WORK_DIR!" mkdir "!WORK_DIR!"

:: ---- Counters ----
set "SUCCESS_COUNT=0"
set "FAIL_COUNT=0"
set "TOTAL_COUNT=0"

:: ============================================================
::  MAIN LOOP — Read CSV line by line
::  Expected CSV format (no header row, or header with #):
::    app_name,image_url
:: ============================================================
call :log "Reading applications from !INPUT_CSV!..."

for /f "usebackq tokens=1,2 delims=," %%A in ("!INPUT_CSV!") do (
    set "ROW_APP=%%~A"
    set "ROW_IMG=%%~B"

    :: Skip blank lines and comment lines
    if "!ROW_APP!"=="" goto :continue_csv
    set "FIRST_CHAR=!ROW_APP:~0,1!"
    if "!FIRST_CHAR!"=="#" goto :continue_csv

    :: Skip header row if present
    echo !ROW_APP! | findstr /i "app_name application name" >nul 2>&1
    if not errorlevel 1 goto :continue_csv

    set /a TOTAL_COUNT+=1
    call :process_app "!ROW_APP!" "!ROW_IMG!"

    :continue_csv
)

echo.
call :log "============================================================"
call :log "All rows processed."
call :log "  Total   : !TOTAL_COUNT!"
call :log "  Success : !SUCCESS_COUNT!"
call :log "  Failed  : !FAIL_COUNT!"
call :log "============================================================"
echo.
echo  Done! Check !LOG_FILE! for details.
goto :eof


:: ============================================================
::  SUBROUTINE: process_app  APP_NAME  IMAGE_URL
:: ============================================================
:process_app
    set "APP_RAW=%~1"
    set "IMAGE=%~2"

    :: Sanitize app name: replace non-alnum with dash
    set "APP_NAME="
    call :sanitize_name "!APP_RAW!"

    if "!APP_NAME!"=="" (
        call :log "ERROR: Empty app name from '!APP_RAW!'. Skipping."
        set /a FAIL_COUNT+=1
        goto :eof
    )
    if "!IMAGE!"=="" (
        call :log "ERROR: No image for '!APP_NAME!'. Skipping."
        set /a FAIL_COUNT+=1
        goto :eof
    )

    set "APP_WORK=!WORK_DIR!\!APP_NAME!"
    set "BIN_DIR=!APP_WORK!\binaries"
    set "TAR_DUMP=!APP_WORK!\fs.tar"
    set "CONTAINER_ID="

    call :log "==========================================="
    call :log "Processing : !APP_NAME!"
    call :log "Image      : !IMAGE!"
    call :log "==========================================="

    if not exist "!APP_WORK!" mkdir "!APP_WORK!"
    if not exist "!BIN_DIR!" mkdir "!BIN_DIR!"

    :: --------------------------------------------------------
    ::  STEP 1 — Pull & Create a temporary container
    :: --------------------------------------------------------
    call :log "[Step 1] Pulling image and creating temporary container..."

    for /f "delims=" %%C in ('docker create "!IMAGE!" 2^>^&1') do (
        set "CREATE_OUT=%%C"
    )

    :: If output looks like a valid container ID (64 hex chars), it succeeded
    echo !CREATE_OUT! | findstr /r "^[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]" >nul 2>&1
    if errorlevel 1 (
        call :log "ERROR: docker create failed for !IMAGE!: !CREATE_OUT!"
        set /a FAIL_COUNT+=1
        goto :eof
    )
    set "CONTAINER_ID=!CREATE_OUT!"
    call :log "  Container created: !CONTAINER_ID:~0,12!..."

    :: --------------------------------------------------------
    ::  STEP 2 — Export container filesystem and extract binaries
    :: --------------------------------------------------------
    call :log "[Step 2] Exporting container filesystem..."

    docker export "!CONTAINER_ID!" -o "!TAR_DUMP!" 2>&1
    if errorlevel 1 (
        call :log "ERROR: docker export failed for !CONTAINER_ID!"
        call :cleanup_container "!CONTAINER_ID!"
        set /a FAIL_COUNT+=1
        goto :eof
    )
    call :log "  Filesystem exported to !TAR_DUMP!"

    call :log "  Scanning for JAR/WAR/EAR files..."
    set "FOUND_COUNT=0"
    set "FIRST_ARTIFACT="

    :: List all .jar/.war/.ear entries in the tar
    :: tar tf outputs one file per line; we filter with findstr
    for /f "delims=" %%F in ('tar tf "!TAR_DUMP!" 2^>nul ^| findstr /i "\.jar$\|\.war$\|\.ear$"') do (
        set "ENTRY=%%F"

        :: Check against ignore patterns
        set "SKIP_ENTRY=0"
        for %%I in (!IGNORE_PATTERNS!) do (
            echo !ENTRY! | findstr /i "%%I" >nul 2>&1
            if not errorlevel 1 set "SKIP_ENTRY=1"
        )

        if "!SKIP_ENTRY!"=="0" (
            set "FNAME=%%~nxF"
            call :log "  -> Discovered artifact: !ENTRY!"

            :: Extract only this specific file from the tar
            tar xf "!TAR_DUMP!" -C "!BIN_DIR!" "!ENTRY!" 2>nul

            :: The extracted file may be nested; find and move it flat
            for /r "!BIN_DIR!" %%X in ("!FNAME!") do (
                if not "%%~dpX"=="!BIN_DIR!\" (
                    copy /y "%%X" "!BIN_DIR!\!FNAME!" >nul 2>&1
                    del /q "%%X" >nul 2>&1
                )
            )

            if "!FIRST_ARTIFACT!"=="" set "FIRST_ARTIFACT=!FNAME!"
            set /a FOUND_COUNT+=1
        )
    )

    :: Fallback: try /app.jar directly
    if "!FOUND_COUNT!"=="0" (
        call :log "  No artifacts via tar scan. Trying fallback /app.jar..."
        docker cp "!CONTAINER_ID!:/app.jar" "!BIN_DIR!\app.jar" >nul 2>&1
        if not errorlevel 1 (
            if exist "!BIN_DIR!\app.jar" (
                call :log "  Fallback: extracted /app.jar"
                set "FIRST_ARTIFACT=app.jar"
                set "FOUND_COUNT=1"
            )
        )
    )

    if "!FOUND_COUNT!"=="0" (
        call :log "ERROR: No JAR/WAR/EAR found in container !IMAGE!. Skipping."
        call :cleanup_step "!CONTAINER_ID!" "!IMAGE!" "!APP_WORK!"
        set /a FAIL_COUNT+=1
        goto :eof
    )
    call :log "  Found !FOUND_COUNT! artifact(s). Primary: !FIRST_ARTIFACT!"

    :: Remove the large tar dump to save disk space
    del /q "!TAR_DUMP!" >nul 2>&1

    :: --------------------------------------------------------
    ::  STEP 3 — Delete existing MTA app (idempotency)
    :: --------------------------------------------------------
    call :log "[Step 3] Checking for existing MTA application '!APP_NAME!'..."
    call :get_existing_app_id "!APP_NAME!"

    if defined EXISTING_APP_ID (
        call :log "  Found existing app ID=!EXISTING_APP_ID!. Deleting for clean re-run..."
        if defined MTA_TOKEN (
            curl -sk -X DELETE -H "Authorization: Bearer !MTA_TOKEN!" "!MTA_URL!/hub/applications/!EXISTING_APP_ID!" >nul 2>&1
        ) else (
            curl -sk -X DELETE "!MTA_URL!/hub/applications/!EXISTING_APP_ID!" >nul 2>&1
        )
        timeout /t 2 /nobreak >nul
    )

    :: --------------------------------------------------------
    ::  STEP 4 — Create new MTA Application
    :: --------------------------------------------------------
    call :log "[Step 4] Creating MTA Application '!APP_NAME!'..."

    set "APP_DESC=Docker Image: !IMAGE!"
    set "APP_PAYLOAD={\"name\":\"!APP_NAME!\",\"description\":\"!APP_DESC!\"}"

    set "CREATE_RESP_FILE=%TEMP%\mta_app_create_!APP_NAME!.json"

    if defined MTA_TOKEN (
        curl -sk -X POST ^
            -H "Authorization: Bearer !MTA_TOKEN!" ^
            -H "Content-Type: application/json" ^
            -d "!APP_PAYLOAD!" ^
            -o "!CREATE_RESP_FILE!" ^
            "!MTA_URL!/hub/applications"
    ) else (
        curl -sk -X POST ^
            -H "Content-Type: application/json" ^
            -d "!APP_PAYLOAD!" ^
            -o "!CREATE_RESP_FILE!" ^
            "!MTA_URL!/hub/applications"
    )

    :: Extract app ID from JSON response using findstr + token parsing
    call :extract_json_id "!CREATE_RESP_FILE!" APP_ID

    if "!APP_ID!"=="" (
        call :log "ERROR: Failed to create MTA application for !APP_NAME!."
        if exist "!CREATE_RESP_FILE!" (
            set /p RESP_BODY=<"!CREATE_RESP_FILE!"
            call :log "  Server response: !RESP_BODY!"
        )
        call :cleanup_step "!CONTAINER_ID!" "!IMAGE!" "!APP_WORK!"
        set /a FAIL_COUNT+=1
        goto :eof
    )
    call :log "  Application created: ID=!APP_ID!"

    :: --------------------------------------------------------
    ::  STEP 5 — Upload binaries to MTA application bucket
    :: --------------------------------------------------------
    call :log "[Step 5] Uploading binaries to application bucket (ID=!APP_ID!)..."
    set "UPLOAD_COUNT=0"

    for %%B in ("!BIN_DIR!\*.jar" "!BIN_DIR!\*.war" "!BIN_DIR!\*.ear") do (
        if exist "%%B" (
            set "B_FILE=%%~nxB"
            set "B_PATH=%%B"
            call :log "  -> Uploading: !B_FILE!"

            set "UPLOAD_RESP_FILE=%TEMP%\mta_upload_!APP_NAME!_!B_FILE!.json"

            if defined MTA_TOKEN (
                curl -sk -X POST ^
                    -H "Authorization: Bearer !MTA_TOKEN!" ^
                    -F "file=@\"!B_PATH!\";type=application/java-archive" ^
                    -o "!UPLOAD_RESP_FILE!" ^
                    "!MTA_URL!/hub/applications/!APP_ID!/bucket/binary/"
            ) else (
                curl -sk -X POST ^
                    -F "file=@\"!B_PATH!\";type=application/java-archive" ^
                    -o "!UPLOAD_RESP_FILE!" ^
                    "!MTA_URL!/hub/applications/!APP_ID!/bucket/binary/"
            )

            if errorlevel 1 (
                call :log "  WARNING: Upload failed for !B_FILE!"
            ) else (
                call :log "  Upload successful: !B_FILE!"
                set /a UPLOAD_COUNT+=1
                :: Track first artifact for task creation
                if "!FIRST_ARTIFACT!"=="!B_FILE!" set "UPLOADED_PRIMARY=!B_FILE!"
            )
        )
    )

    if "!UPLOAD_COUNT!"=="0" (
        call :log "ERROR: No files uploaded for !APP_NAME!. Skipping task creation."
        call :cleanup_step "!CONTAINER_ID!" "!IMAGE!" "!APP_WORK!"
        set /a FAIL_COUNT+=1
        goto :eof
    )
    call :log "  !UPLOAD_COUNT! file(s) uploaded successfully."

    :: Use first artifact as the analysis target
    if "!UPLOADED_PRIMARY!"=="" set "UPLOADED_PRIMARY=!FIRST_ARTIFACT!"
    set "HUB_ARTIFACT_PATH=/hub/applications/!APP_ID!/bucket/binary/!UPLOADED_PRIMARY!"

    :: --------------------------------------------------------
    ::  STEP 6 — Create Analysis Task
    :: --------------------------------------------------------
    call :log "[Step 6] Creating Analysis Task for !APP_NAME! (target: !TARGETS!)..."

    set "TASK_NAME=!APP_NAME!-analysis"
    set "TASK_FILE=%TEMP%\mta_task_!APP_NAME!.json"
    set "TASK_RESP_FILE=%TEMP%\mta_task_resp_!APP_NAME!.json"

    :: Build targets JSON array from comma-separated TARGETS
    call :csv_to_json_array "!TARGETS!" TARGETS_JSON

    :: Write task JSON to temp file (avoids CMD quoting nightmares)
    (
        echo {
        echo   "name": "!TASK_NAME!",
        echo   "state": "Ready",
        echo   "addon": "analyzer",
        echo   "application": {"id": !APP_ID!},
        echo   "data": {
        echo     "mode": {
        echo       "artifact": "!HUB_ARTIFACT_PATH!",
        echo       "binary": true,
        echo       "withDeps": false,
        echo       "diva": false
        echo     },
        echo     "targets": !TARGETS_JSON!,
        echo     "sources": [],
        echo     "scope": {"withKnown": false},
        echo     "rules": {"path": "", "tags": []}
        echo   }
        echo }
    ) > "!TASK_FILE!"

    if defined MTA_TOKEN (
        curl -sk -X POST ^
            -H "Authorization: Bearer !MTA_TOKEN!" ^
            -H "Content-Type: application/json" ^
            -d "@!TASK_FILE!" ^
            -o "!TASK_RESP_FILE!" ^
            "!MTA_URL!/hub/tasks"
    ) else (
        curl -sk -X POST ^
            -H "Content-Type: application/json" ^
            -d "@!TASK_FILE!" ^
            -o "!TASK_RESP_FILE!" ^
            "!MTA_URL!/hub/tasks"
    )

    call :extract_json_id "!TASK_RESP_FILE!" TASK_ID

    if "!TASK_ID!"=="" (
        call :log "ERROR: Analysis task creation failed for !APP_NAME!."
        if exist "!TASK_RESP_FILE!" (
            set /p TRESP=<"!TASK_RESP_FILE!"
            call :log "  Server response: !TRESP!"
        )
        call :cleanup_step "!CONTAINER_ID!" "!IMAGE!" "!APP_WORK!"
        set /a FAIL_COUNT+=1
        goto :eof
    )

    call :log "  Analysis Task created: ID=!TASK_ID! (State=Ready -> queued)"
    call :log "SUCCESS: !APP_NAME! fully processed. App ID=!APP_ID! Task ID=!TASK_ID!"
    set /a SUCCESS_COUNT+=1

    :: Cleanup temp JSON files
    del /q "!TASK_FILE!" >nul 2>&1
    del /q "!TASK_RESP_FILE!" >nul 2>&1
    del /q "!CREATE_RESP_FILE!" >nul 2>&1

    call :cleanup_step "!CONTAINER_ID!" "!IMAGE!" "!APP_WORK!"
    goto :eof


:: ============================================================
::  SUBROUTINE: cleanup_step  CONTAINER_ID  IMAGE  WORK_DIR
:: ============================================================
:cleanup_step
    set "CL_CID=%~1"
    set "CL_IMG=%~2"
    set "CL_DIR=%~3"

    if not "!CL_CID!"=="" (
        docker rm -f "!CL_CID!" >nul 2>&1
        call :log "  [Cleanup] Container !CL_CID:~0,12! removed."
    )

    if "!KEEP_ARTIFACTS!"=="0" (
        if defined CL_IMG (
            docker rmi -f "!CL_IMG!" >nul 2>&1
            call :log "  [Cleanup] Image !CL_IMG! removed from local cache."
        )
        if exist "!CL_DIR!" (
            rd /s /q "!CL_DIR!" >nul 2>&1
            call :log "  [Cleanup] Work dir !CL_DIR! removed."
        )
    )
    goto :eof


:: ============================================================
::  SUBROUTINE: cleanup_container  CONTAINER_ID
:: ============================================================
:cleanup_container
    docker rm -f "%~1" >nul 2>&1
    goto :eof


:: ============================================================
::  SUBROUTINE: get_existing_app_id  APP_NAME
::  Sets EXISTING_APP_ID if found, clears it otherwise
:: ============================================================
:get_existing_app_id
    set "FIND_NAME=%~1"
    set "EXISTING_APP_ID="
    set "APPS_RESP=%TEMP%\mta_apps_list.json"

    if defined MTA_TOKEN (
        curl -sk -H "Authorization: Bearer !MTA_TOKEN!" -o "!APPS_RESP!" "!MTA_URL!/hub/applications" 2>nul
    ) else (
        curl -sk -o "!APPS_RESP!" "!MTA_URL!/hub/applications" 2>nul
    )

    if not exist "!APPS_RESP!" goto :eof

    :: Parse JSON array: find the id next to the name field matching APP_NAME
    :: Strategy: grep for the name, then find nearest id
    set "FOUND_NAME_LINE=0"
    for /f "tokens=* delims=" %%L in ('type "!APPS_RESP!" 2^>nul') do (
        set "LINE=%%L"
        echo !LINE! | findstr /i "\"name\"" >nul 2>&1
        if not errorlevel 1 (
            echo !LINE! | findstr /i "\"!FIND_NAME!\"" >nul 2>&1
            if not errorlevel 1 set "FOUND_NAME_LINE=1"
        )
        if "!FOUND_NAME_LINE!"=="1" (
            echo !LINE! | findstr /r "\"id\":[0-9]" >nul 2>&1
            if not errorlevel 1 (
                for /f "tokens=2 delims=:" %%I in ("!LINE!") do (
                    set "RAW_ID=%%I"
                    set "RAW_ID=!RAW_ID: =!"
                    set "RAW_ID=!RAW_ID:,=!"
                    set "RAW_ID=!RAW_ID:}=!"
                    set "EXISTING_APP_ID=!RAW_ID!"
                )
                set "FOUND_NAME_LINE=0"
            )
        )
    )
    goto :eof


:: ============================================================
::  SUBROUTINE: extract_json_id  JSON_FILE  OUTPUT_VAR
::  Extracts the first "id": <number> from a JSON file
:: ============================================================
:extract_json_id
    set "JSON_FILE=%~1"
    set "OUT_VAR=%~2"
    set "!OUT_VAR!="

    if not exist "!JSON_FILE!" goto :eof

    for /f "tokens=* delims=" %%L in ('type "!JSON_FILE!" 2^>nul') do (
        set "JLINE=%%L"
        echo !JLINE! | findstr /r "\"id\"[ ]*:[ ]*[0-9]" >nul 2>&1
        if not errorlevel 1 (
            :: Extract numeric value after "id":
            for /f "tokens=2 delims=:" %%V in ("!JLINE!") do (
                set "RAW=%%V"
                :: Strip spaces, commas, braces, quotes
                set "RAW=!RAW: =!"
                set "RAW=!RAW:,=!"
                set "RAW=!RAW:}=!"
                set "RAW=!RAW:{=!"
                set "RAW=!RAW:"=!"
                :: Keep only digits
                for /f "delims=0123456789" %%D in ("!RAW!") do set "RAW=!RAW:%%D=!"
                if not "!RAW!"=="" set "!OUT_VAR!=!RAW!"
            )
            goto :eof
        )
    )
    goto :eof


:: ============================================================
::  SUBROUTINE: csv_to_json_array  CSV_STRING  OUTPUT_VAR
::  Converts "a,b,c" -> ["a","b","c"]
:: ============================================================
:csv_to_json_array
    set "CSV_IN=%~1"
    set "JARR_OUT=%~2"
    set "JARR=["
    set "FIRST_ITEM=1"

    :: Replace commas with loop iterations
    set "TEMP_CSV=!CSV_IN!"
    :csv_loop
        for /f "tokens=1* delims=," %%A in ("!TEMP_CSV!") do (
            set "ITEM=%%A"
            set "ITEM=!ITEM: =!"
            if not "!ITEM!"=="" (
                if "!FIRST_ITEM!"=="1" (
                    set "JARR=!JARR!\"!ITEM!\""
                    set "FIRST_ITEM=0"
                ) else (
                    set "JARR=!JARR!,\"!ITEM!\""
                )
            )
            if not "%%B"=="" (
                set "TEMP_CSV=%%B"
                goto :csv_loop
            )
        )
    set "JARR=!JARR!]"
    set "!JARR_OUT!=!JARR!"
    goto :eof


:: ============================================================
::  SUBROUTINE: sanitize_name  RAW_NAME
::  Sets APP_NAME with non-alnum replaced by dash (lowercase)
:: ============================================================
:sanitize_name
    set "RAW=%~1"
    set "APP_NAME="
    set "CHARS=0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
    :: Simple replacement of common special chars
    set "CLEAN=!RAW!"
    set "CLEAN=!CLEAN: =-!"
    set "CLEAN=!CLEAN:_=-!"
    set "CLEAN=!CLEAN:/=-!"
    set "CLEAN=!CLEAN:\=-!"
    set "CLEAN=!CLEAN:.=-!"
    set "CLEAN=!CLEAN::=-!"
    set "CLEAN=!CLEAN:@=-!"
    set "APP_NAME=!CLEAN!"
    goto :eof


:: ============================================================
::  SUBROUTINE: log  MESSAGE
:: ============================================================
:log
    set "MSG=%~1"
    set "TS=%date% %time%"
    echo [%TS%] !MSG!
    echo [%TS%] !MSG!>> "!LOG_FILE!"
    goto :eof
