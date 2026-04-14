@echo off
setlocal EnableDelayedExpansion

:: ============================================================
::  MTA Windows Automation Script  |  v1.0
::  Tools : docker  curl  powershell
::  Input : input.csv  ->  app_name,image_url
:: ============================================================

:: ==============  USER CONFIGURATION  ========================
set "MTA_URL=https://mta-ui.apps.your-cluster.com"
set "INPUT_CSV=input.csv"
set "TARGETS=cloud-readiness"
set "KEEP_ARTIFACTS=0"
:: =============================================================

set "WORK_DIR=%~dp0mta_workspace"
set "LOG_FILE=%~dp0mta_workflow.log"
set "SCRIPT_DIR=%~dp0"

:: CLI overrides: script.bat [MTA_URL] [CSV] [TARGETS]
if not "%~1"=="" set "MTA_URL=%~1"
if not "%~2"=="" set "INPUT_CSV=%~2"
if not "%~3"=="" set "TARGETS=%~3"

:: Resolve CSV to absolute path when given as relative
if not "%INPUT_CSV:~1,1%"==":" set "INPUT_CSV=%SCRIPT_DIR%%INPUT_CSV%"

:: Strip trailing slash from MTA_URL
:strip_slash
if "%MTA_URL:~-1%"=="/" (
    set "MTA_URL=%MTA_URL:~0,-1%"
    goto strip_slash
)

:: --- Init log ------------------------------------------------
echo.>> "%LOG_FILE%"
echo ==========================================================>> "%LOG_FILE%"
echo [%date% %time%] MTA Workflow Started>> "%LOG_FILE%"
echo URL=%MTA_URL% CSV=%INPUT_CSV% TARGETS=%TARGETS%>> "%LOG_FILE%"
echo ==========================================================>> "%LOG_FILE%"

echo.
echo ==========================================================
echo  MTA Windows Automation Script
echo ==========================================================
echo  MTA URL  : %MTA_URL%
echo  CSV File : %INPUT_CSV%
echo  Targets  : %TARGETS%
echo  Auth     : none  (HTTPS insecure / self-signed cert)
echo ==========================================================
echo.

:: --- Pre-flight checks ---------------------------------------
call :log "PRE-FLIGHT: Checking required tools..."

where docker >nul 2>&1
if errorlevel 1 (
    call :log "FATAL: docker not found. Install Docker Desktop."
    exit /b 1
)
call :log "  [OK] docker"

where curl >nul 2>&1
if errorlevel 1 (
    call :log "FATAL: curl not found. Install or add curl to PATH."
    exit /b 1
)
call :log "  [OK] curl"

where powershell >nul 2>&1
if errorlevel 1 (
    call :log "FATAL: powershell not found. Required for JSON parsing."
    exit /b 1
)
call :log "  [OK] powershell"

docker info >nul 2>&1
if errorlevel 1 (
    call :log "  [WARN] Docker daemon not responding - start Docker Desktop."
) else (
    call :log "  [OK] Docker Desktop running."
)

if not exist "%INPUT_CSV%" (
    call :log "FATAL: Input CSV not found: %INPUT_CSV%"
    exit /b 1
)

if not exist "%WORK_DIR%" mkdir "%WORK_DIR%"

:: --- Global counters -----------------------------------------
set "G_TOTAL=0"
set "G_SUCCESS=0"
set "G_FAIL=0"

:: --- CSV Processing Loop -------------------------------------
call :log "Reading: %INPUT_CSV%"
echo.

for /f "usebackq delims=" %%L in (`type "%INPUT_CSV%"`) do (
    set "_LINE=%%L"
    call :trimvar _LINE
    if not "!_LINE!"=="" (
        if not "!_LINE:~0,1!"=="#" (
            for /f "tokens=1* delims=," %%A in ("!_LINE!") do (
                set "_APP=%%~A"
                set "_IMG=%%~B"
                call :trimvar _APP
                call :trimvar _IMG
                if not "!_APP!"=="" if not "!_IMG!"=="" (
                    if /i not "!_APP!"=="app_name" if /i not "!_APP!"=="application_name" (
                        set /a G_TOTAL+=1
                        call :log "----------------------------------------------------------"
                        call :log "Row !G_TOTAL!: App=[!_APP!]  Image=[!_IMG!]"
                        call :process_app "!_APP!" "!_IMG!"
                    )
                )
            )
        )
    )
)

:: --- Final Summary -------------------------------------------
echo.
call :log "=========================================================="
call :log "WORKFLOW COMPLETE"
call :log "  Total     : !G_TOTAL!"
call :log "  Succeeded : !G_SUCCESS!"
call :log "  Failed    : !G_FAIL!"
call :log "=========================================================="
echo.
echo  Log: %LOG_FILE%
exit /b 0

:: ==================================================================
::  SUBROUTINE  process_app  "APP_NAME"  "IMAGE_URL"
:: ==================================================================
:process_app
    set "PA_RAW=%~1"
    set "PA_IMG=%~2"
    set "PA_APP_ID="
    set "PA_TASK_ID="
    set "PA_CID="
    set "PA_FIRST="
    set "PA_FOUND=0"
    set "PA_UPL=0"
    set "PA_PRIM="

    set "PA_APP=%PA_RAW%"
    set "PA_APP=!PA_APP: =-!"
    set "PA_APP=!PA_APP:_=-!"
    set "PA_APP=!PA_APP:/=-!"
    set "PA_APP=!PA_APP:\=-!"
    set "PA_APP=!PA_APP:.=-!"
    set "PA_APP=!PA_APP::=-!"
    set "PA_APP=!PA_APP:@=-!"
    set "PA_APP=!PA_APP:*=-!"
    set "PA_APP=!PA_APP:?=-!"

    set "PA_WDIR=%WORK_DIR%!PA_APP!"
    set "PA_BDIR=!PA_WDIR!\binaries"
    set "PA_TAR=!PA_WDIR!\fs.tar"

    call :log "  App   : !PA_APP!"
    call :log "  Image : !PA_IMG!"

    if not exist "!PA_WDIR!" mkdir "!PA_WDIR!"
    if not exist "!PA_BDIR!" mkdir "!PA_BDIR!"

    call :log "[1/5] Creating container (pulls image if needed)..."
    set "PA_CID="
    for /f "delims=" %%C in ('docker create "!PA_IMG!" 2^>^&1') do (
        if "!PA_CID!"=="" set "PA_CID=%%C"
    )

    if "!PA_CID!"=="" (
        call :log "  ERROR: docker create failed for !PA_IMG!"
        set /a G_FAIL+=1
        goto :proc_end
    )
    echo !PA_CID! | findstr " " >nul 2>&1
    if not errorlevel 1 (
        call :log "  ERROR: docker create returned an error: !PA_CID!"
        set "PA_CID="
        set /a G_FAIL+=1
        goto :proc_end
    )
    call :log "  Container: !PA_CID:~0,12!..."

    call :log "  Exporting filesystem..."
    docker export "!PA_CID!" -o "!PA_TAR!" 2>nul
    if errorlevel 1 (
        call :log "  ERROR: docker export failed."
        set /a G_FAIL+=1
        goto :proc_end
    )

    call :log "  Scanning for JAR/WAR/EAR..."
    set "PA_LIST=%TEMP%\mta_arts_%RANDOM%.txt"
    set "PSARG1=!PA_TAR!"
    set "PSARG2=!PA_LIST!"

    powershell -NoProfile -Command ^
        "$ign=@('WEB-INF/lib','BOOT-INF/lib','usr/lib/jvm','usr/java','jre/lib','jdk/lib','/.m2/','m2/repository','/.gradle/','var/cache/','usr/share/','usr/lib64/','modules/system/','wlp/lib/','tomcat/lib/','apache-tomcat/lib/','/node_modules/','/libs/','/lib/');" ^
        "$arts=(tar tf $env:PSARG1 2>$null)|Where-Object{$_ -match '\.(jar|war|ear)$'}|Where-Object{$e=$_;-not($ign|Where-Object{$e -match [regex]::Escape($_)})};" ^
        "if($arts){$arts|Set-Content -Encoding utf8 $env:PSARG2}" >nul 2>&1

    if exist "!PA_LIST!" (
        for /f "usebackq delims=" %%E in ("!PA_LIST!") do (
            set "PA_ENTRY=%%E"
            if not "!PA_ENTRY!"=="" (
                for %%N in ("!PA_ENTRY!") do set "PA_FNAME=%%~nxN"
                call :log "    Found: !PA_ENTRY!"
                tar xf "!PA_TAR!" -C "!PA_BDIR!" "!PA_ENTRY!" 2>nul
                if not exist "!PA_BDIR!\!PA_FNAME!" (
                    for /r "!PA_BDIR!" %%X in ("!PA_FNAME!") do (
                        if exist "%%X" move /y "%%X" "!PA_BDIR!\!PA_FNAME!" >nul 2>&1
                    )
                )
                if "!PA_FIRST!"=="" set "PA_FIRST=!PA_FNAME!"
                set /a PA_FOUND+=1
            )
        )
        del /q "!PA_LIST!" >nul 2>&1
    )

    if "!PA_FOUND!"=="0" (
        call :log "  Tar scan found nothing. Trying /app.jar fallback..."
        docker cp "!PA_CID!:/app.jar" "!PA_BDIR!\app.jar" >nul 2>&1
        if exist "!PA_BDIR!\app.jar" (
            call :log "  Fallback OK: /app.jar extracted."
            set "PA_FIRST=app.jar"
            set "PA_FOUND=1"
        )
    )

    if "!PA_FOUND!"=="0" (
        call :log "  ERROR: No JAR/WAR/EAR found in !PA_IMG!"
        call :log "  TIP: docker run --rm !PA_IMG! find / -name *.jar 2>/dev/null"
        set /a G_FAIL+=1
        goto :proc_end
    )
    call :log "  Found !PA_FOUND! artifact(s). Primary: !PA_FIRST!"
    del /q "!PA_TAR!" >nul 2>&1

    call :log "[2/5] Creating MTA Application..."
    set "PA_APPS=%TEMP%\mta_apps_%RANDOM%.json"
    curl -sk --max-time 30 -o "!PA_APPS!" "%MTA_URL%/hub/applications" >nul 2>&1
    set "PA_OLD_ID="
    if exist "!PA_APPS!" (
        set "PSARG1=!PA_APPS!"
        set "PSARG2=!PA_APP!"
        for /f "delims=" %%I in ('powershell -NoProfile -Command ^
            "try{$j=Get-Content -Raw $env:PSARG1|ConvertFrom-Json;($j|Where-Object{$_.name -eq $env:PSARG2}|Select-Object -First 1).id}catch{}" 2^>nul') do (
            set "PA_OLD_ID=%%I"
        )
        del /q "!PA_APPS!" >nul 2>&1
    )

    if not "!PA_OLD_ID!"=="" (
        call :log "  Removing existing app ID=!PA_OLD_ID! (idempotency)..."
        curl -sk --max-time 30 -X DELETE "%MTA_URL%/hub/applications/!PA_OLD_ID!" >nul 2>&1
        timeout /t 2 /nobreak >nul 2>&1
    ) else (
        call :log "  No existing app found."
    )

    set "PA_BFILE=%TEMP%\mta_body_%RANDOM%.json"
    set "PA_CRSP=%TEMP%\mta_create_%RANDOM%.json"
    > "!PA_BFILE!" echo {"name":"!PA_APP!","description":"Image: !PA_IMG!"}
    curl -sk --max-time 30 -X POST ^
        -H "Content-Type: application/json" ^
        -d "@!PA_BFILE!" ^
        -o "!PA_CRSP!" ^
        "%MTA_URL%/hub/applications" >nul 2>&1
    del /q "!PA_BFILE!" >nul 2>&1

    set "PA_APP_ID="
    if exist "!PA_CRSP!" (
        set "PSARG1=!PA_CRSP!"
        for /f "delims=" %%I in ('powershell -NoProfile -Command ^
            "try{(Get-Content -Raw $env:PSARG1|ConvertFrom-Json).id}catch{}" 2^>nul') do (
            set "PA_APP_ID=%%I"
        )
        if "!PA_APP_ID!"=="" (
            set /p _ERR=<"!PA_CRSP!"
            call :log "  ERROR: App creation failed. Server: !_ERR!"
        )
        del /q "!PA_CRSP!" >nul 2>&1
    )

    if "!PA_APP_ID!"=="" (
        set /a G_FAIL+=1
        goto :proc_end
    )
    call :log "  Application created: ID=!PA_APP_ID!"

    call :log "[3/5] Creating Analysis Task..."
    set "PA_ARTIFACT=/binary/!PA_FIRST!"
    set "PA_TARR=["
    set "_TFIRST=1"
    for %%T in ("!TARGETS:,=" "!") do (
        set "_TV=%%~T"
        for /f "tokens=* delims= " %%S in ("!_TV!") do set "_TV=%%S"
        if not "!_TV!"=="" (
            if "!_TFIRST!"=="1" (
                set "PA_TARR=!PA_TARR!""!_TV!"""
                set "_TFIRST=0"
            ) else (
                set "PA_TARR=!PA_TARR!,""!_TV!"""
            )
        )
    )
    set "PA_TARR=!PA_TARR!]"

    set "PA_TFILE=%TEMP%\mta_task_%RANDOM%.json"
    set "PA_TRSP=%TEMP%\mta_taskr_%RANDOM%.json"
    (
        echo {
        echo   "name": "!PA_APP!-analysis",
        echo   "state": "Created",
        echo   "addon": "analyzer",
        echo   "application": {"id": !PA_APP_ID!},
        echo   "data": {
        echo     "mode": {
        echo       "artifact": "!PA_ARTIFACT!",
        echo       "binary": true,
        echo       "withDeps": false,
        echo       "diva": false
        echo     },
        echo     "targets": !PA_TARR!,
        echo     "sources": [],
        echo     "scope": {"withKnown": false},
        echo     "rules": {"path": "", "tags": []}
        echo   }
        echo }
    ) > "!PA_TFILE!"

    curl -sk --max-time 60 -X POST ^
        -H "Content-Type: application/json" ^
        -d "@!PA_TFILE!" ^
        -o "!PA_TRSP!" ^
        "%MTA_URL%/hub/tasks" >nul 2>&1
    del /q "!PA_TFILE!" >nul 2>&1

    set "PA_TASK_ID="
    if exist "!PA_TRSP!" (
        set "PSARG1=!PA_TRSP!"
        for /f "delims=" %%I in ('powershell -NoProfile -Command ^
            "try{(Get-Content -Raw $env:PSARG1|ConvertFrom-Json).id}catch{}" 2^>^nul') do (
            set "PA_TASK_ID=%%I"
        )
        if "!PA_TASK_ID!"=="" (
            set /p _TERR=<"!PA_TRSP!"
            call :log "  ERROR: Task creation failed. Server: !_TERR!"
        )
        del /q "!PA_TRSP!" >nul 2>&1
    )

    if "!PA_TASK_ID!"=="" (
        call :log "  ERROR: Could not get task ID. Aborting !PA_APP!."
        curl -sk --max-time 30 -X DELETE "%MTA_URL%/hub/applications/!PA_APP_ID!" >nul 2>&1
        set /a G_FAIL+=1
        goto :proc_end
    )
    call :log "  Task created: ID=!PA_TASK_ID!"

    call :log "[4/5] Uploading binaries..."
    set "PA_BASE=%MTA_URL%/hub/tasks/!PA_TASK_ID!/bucket/binary"
    for %%X in (jar war ear) do (
        for %%B in ("!PA_BDIR!\*.%%X") do (
            if exist "%%~fB" (
                set "PA_BNAME=%%~nxB"
                set "PA_BPATH=%%~fB"
                call :log "  -> !PA_BNAME!"
                curl -sk --max-time 300 -X PUT ^
                    -H "Content-Type: application/octet-stream" ^
                    --data-binary "@!PA_BPATH!" ^
                    "!PA_BASE!/!PA_BNAME!" >nul 2>&1
                if errorlevel 1 (
                    call :log "     WARNING: upload failed for !PA_BNAME!"
                ) else (
                    call :log "     OK: !PA_BNAME! uploaded."
                    set /a PA_UPL+=1
                    if "!PA_PRIM!"=="" set "PA_PRIM=!PA_BNAME!"
                )
            )
        )
    )

    if "!PA_UPL!"=="0" (
        call :log "  ERROR: No files uploaded for !PA_APP!."
        curl -sk --max-time 30 -X DELETE "%MTA_URL%/hub/tasks/!PA_TASK_ID!" >nul 2>&1
        curl -sk --max-time 30 -X DELETE "%MTA_URL%/hub/applications/!PA_APP_ID!" >nul 2>&1
        set /a G_FAIL+=1
        goto :proc_end
    )
    call :log "  !PA_UPL! file(s) uploaded."

    call :log "[5/5] Submitting task..."
    set "PA_SFILE=%TEMP%\mta_sub_%RANDOM%.json"
    set "PA_SRSP=%TEMP%\mta_subr_%RANDOM%.json"
    > "!PA_SFILE!" echo {"state":"Ready"}
    curl -sk --max-time 30 -X PUT ^
        -H "Content-Type: application/json" ^
        -d "@!PA_SFILE!" ^
        -o "!PA_SRSP!" ^
        "%MTA_URL%/hub/tasks/!PA_TASK_ID!/submit" >nul 2>&1

    set "PA_SOK=1"
    if errorlevel 1 (
        call :log "  WARNING: submit request returned non-zero."
        set "PA_SOK=0"
    )
    del /q "!PA_SFILE!" >nul 2>&1
    if exist "!PA_SRSP!" (
        findstr /i "error" "!PA_SRSP!" >nul 2>&1
        if not errorlevel 1 (
            set /p _SERR=<"!PA_SRSP!"
            call :log "  WARNING: submit response contains error: !_SERR!"
            set "PA_SOK=0"
        )
        del /q "!PA_SRSP!" >nul 2>&1
    )

    if "!PA_SOK!"=="0" (
        call :log "  ERROR: Task submit failed for !PA_APP!."
        set /a G_FAIL+=1
        goto :proc_end
    )

    call :log "  Task submitted: ID=!PA_TASK_ID!"
    call :log "SUCCESS: !PA_APP! | AppID=!PA_APP_ID! | TaskID=!PA_TASK_ID! | Files=!PA_UPL!"
    set /a G_SUCCESS+=1

:proc_end
    call :cleanup "!PA_CID!" "!PA_IMG!" "!PA_WDIR!"
    goto :eof

:: ==================================================================
::  SUBROUTINE  cleanup  "CONTAINER_ID" "IMAGE" "WORK_DIR"
:: ==================================================================
:cleanup
    if not "%~1"=="" docker rm -f "%~1" >nul 2>&1
    if "%KEEP_ARTIFACTS%"=="0" (
        if not "%~2"=="" docker rmi -f "%~2" >nul 2>&1
        if exist "%~3" rd /s /q "%~3" >nul 2>&1
    )
    goto :eof

:: ==================================================================
::  SUBROUTINE  trimvar  "VARIABLE_NAME"
:: ==================================================================
:trimvar
    set "VAR_NAME=%~1"
    set "VALUE=!%VAR_NAME%!"
    for /f "tokens=* delims= " %%T in ("!VALUE!") do set "VALUE=%%T"
:trimvar_loop
    if "!VALUE:~-1!"==" " (
        set "VALUE=!VALUE:~0,-1!"
        goto trimvar_loop
    )
    set "%VAR_NAME%=!VALUE!"
    goto :eof

:: ==================================================================
::  SUBROUTINE  log  "MESSAGE"
:: ==================================================================
:log
    echo [%date% %time%] %~1
    echo [%date% %time%] %~1>> "%LOG_FILE%"
    goto :eof
@echo off
setlocal EnableDelayedExpansion

:: ============================================================
::  MTA 8.0.1  |  Windows Automation Script  |  v3.2
::  Tools : docker  curl  powershell  (built-in to Windows 10+)
::  Input : input.csv  ->  app_name,image_url
::
::  Assumptions:
::    - MTA Hub has NO authentication enabled
::    - HTTPS with self-signed cert  (curl -sk used everywhere)
::
::  WORKFLOW PER ROW:
::    [1] docker create/export -> scan tar -> extract JAR/WAR/EAR
::    [2] POST /hub/applications          -> create app, get app_id
::    [3] POST /hub/tasks  state=Created  -> stage task, get task_id
::    [4] PUT  /hub/tasks/{id}/bucket/binary/{file} -> upload binary
::    [5] PUT  /hub/tasks/{id}/submit     -> set state=Ready
:: ============================================================

:: ==============  USER CONFIGURATION  ========================
set "MTA_URL=https://mta-ui.apps.your-cluster.com"
set "INPUT_CSV=input.csv"
set "TARGETS=cloud-readiness"
set "KEEP_ARTIFACTS=0"
:: =============================================================

set "WORK_DIR=%~dp0mta_workspace"
set "LOG_FILE=%~dp0mta_workflow.log"
set "SCRIPT_DIR=%~dp0"

:: CLI overrides: script.bat [URL] [CSV] [TARGETS]
if not "%~1"=="" set "MTA_URL=%~1"
if not "%~2"=="" set "INPUT_CSV=%~2"
if not "%~3"=="" set "TARGETS=%~3"

:: Resolve CSV to absolute path when given as relative
if not "!INPUT_CSV:~1,1!"==":" set "INPUT_CSV=!SCRIPT_DIR!!INPUT_CSV!"

:: Strip trailing slash from MTA_URL
:strip_slash
if "!MTA_URL:~-1!"=="/" (
    set "MTA_URL=!MTA_URL:~0,-1!"
    goto strip_slash
)

:: --- Init log ------------------------------------------------
echo.>> "!LOG_FILE!"
echo ==========================================================>> "!LOG_FILE!"
echo [%date% %time%] MTA Workflow v3.2 Started>> "!LOG_FILE!"
echo URL=!MTA_URL! CSV=!INPUT_CSV! TARGETS=!TARGETS!>> "!LOG_FILE!"
echo ==========================================================>> "!LOG_FILE!"

echo.
echo ==========================================================
echo  Red Hat MTA 8.0.1 ^| Windows Automation Script v3.2
echo ==========================================================
echo  MTA URL  : !MTA_URL!
echo  CSV File : !INPUT_CSV!
echo  Targets  : !TARGETS!
echo  Auth     : none  ^(HTTPS insecure / self-signed cert^)
echo ==========================================================
echo.

:: --- Pre-flight checks ---------------------------------------
call :log "PRE-FLIGHT: Checking required tools..."

where docker >nul 2>&1
if errorlevel 1 (
    call :log "FATAL: docker not found. Install Docker Desktop."
    exit /b 1
)
call :log "  [OK] docker"

where curl >nul 2>&1
if errorlevel 1 (
    call :log "FATAL: curl not found. Requires Windows 10 build 1803+."
    exit /b 1
)
call :log "  [OK] curl"

where powershell >nul 2>&1
if errorlevel 1 (
    call :log "FATAL: powershell not found. Required for JSON parsing."
    exit /b 1
)
call :log "  [OK] powershell"

docker info >nul 2>&1
if errorlevel 1 (
    call :log "  [WARN] Docker daemon not responding - start Docker Desktop."
) else (
    call :log "  [OK] Docker Desktop running"
)

if not exist "!INPUT_CSV!" (
    call :log "FATAL: Input CSV not found: !INPUT_CSV!"
    exit /b 1
)

if not exist "!WORK_DIR!" mkdir "!WORK_DIR!"

:: --- Test MTA API --------------------------------------------
call :log "PRE-FLIGHT: Testing MTA API connectivity..."
set "_PING=%TEMP%\mta_ping_%RANDOM%.tmp"
curl -sk --max-time 10 -o "!_PING!" "!MTA_URL!/hub/applications" >nul 2>&1
if exist "!_PING!" (
    call :log "  [OK] MTA API reachable."
    del /q "!_PING!" >nul 2>&1
) else (
    call :log "  [WARN] MTA API may be unreachable. Proceeding anyway..."
)

:: --- Global counters -----------------------------------------
set "G_TOTAL=0"
set "G_SUCCESS=0"
set "G_FAIL=0"

:: --- CSV Processing Loop -------------------------------------
:: IMPORTANT: use `type "file"` (command-output mode) not in("file")
:: (file-read mode).  Only command-output mode strips trailing \r from
:: each line.  Without this, the last token on every CRLF line (the
:: image URL) keeps its \r, which corrupts docker create calls and
:: makes console output look garbled (cursor jumps to column 0).
call :log "Reading: !INPUT_CSV!"
echo.

for /f "usebackq delims=" %%L in (`type "!INPUT_CSV!"`) do (
    set "_LINE=%%L"

    :: Trim leading and trailing whitespace before processing the line
    for /f "tokens=* delims= " %%T in ("!_LINE!") do set "_LINE=%%T"
    call :trimvar _LINE
    if not "!_LINE!"=="" (
        if not "!_LINE:~0,1!"=="#" (
            for /f "tokens=1* delims=," %%A in ("!_LINE!") do (
                set "_APP=%%~A"
                set "_IMG=%%~B"

                for /f "tokens=* delims= " %%X in ("!_APP!") do set "_APP=%%X"
                for /f "tokens=* delims= " %%X in ("!_IMG!") do set "_IMG=%%X"
                call :trimvar _APP
                call :trimvar _IMG

                if not "!_APP!"=="" if not "!_IMG!"=="" (
                    if /i not "!_APP!"=="app_name" if /i not "!_APP!"=="application_name" (
                        set /a G_TOTAL+=1
                        call :log "----------------------------------------------------------"
                        call :log "Row !G_TOTAL!: App=[!_APP!]  Image=[!_IMG!]"
                        call :process_app "!_APP!" "!_IMG!"
                    )
                )
            )
        )
    )
)

:: --- Final Summary -------------------------------------------
echo.
call :log "=========================================================="
call :log "WORKFLOW COMPLETE"
call :log "  Total     : !G_TOTAL!"
call :log "  Succeeded : !G_SUCCESS!"
call :log "  Failed    : !G_FAIL!"
call :log "=========================================================="
echo.
echo  Log: !LOG_FILE!
exit /b 0


:: ==================================================================
::  SUBROUTINE  process_app  "APP_NAME"  "IMAGE_URL"
:: ==================================================================
:process_app
    set "PA_RAW=%~1"
    set "PA_IMG=%~2"
    set "PA_APP_ID="
    set "PA_TASK_ID="
    set "PA_CID="
    set "PA_FIRST="
    set "PA_FOUND=0"
    set "PA_UPL=0"
    set "PA_PRIM="

    :: Sanitize app name: replace special chars with dash
    set "PA_APP=!PA_RAW!"
    set "PA_APP=!PA_APP: =-!"
    set "PA_APP=!PA_APP:_=-!"
    set "PA_APP=!PA_APP:/=-!"
    set "PA_APP=!PA_APP:\=-!"
    set "PA_APP=!PA_APP:.=-!"
    set "PA_APP=!PA_APP::=-!"
    set "PA_APP=!PA_APP:@=-!"
    set "PA_APP=!PA_APP:*=-!"
    set "PA_APP=!PA_APP:?=-!"

    set "PA_WDIR=!WORK_DIR!\!PA_APP!"
    set "PA_BDIR=!PA_WDIR!\binaries"
    set "PA_TAR=!PA_WDIR!\fs.tar"

    call :log "  App   : !PA_APP!"
    call :log "  Image : !PA_IMG!"

    if not exist "!PA_WDIR!" mkdir "!PA_WDIR!"
    if not exist "!PA_BDIR!" mkdir "!PA_BDIR!"

    :: ============================================================
    ::  [1/5]  docker create -> export -> extract JARs/WARs/EARs
    :: ============================================================
    call :log "[1/5] Creating container (pulls image if not cached)..."

    set "PA_CID="
    for /f "delims=" %%C in ('docker create "!PA_IMG!" 2^>^&1') do (
        if "!PA_CID!"=="" set "PA_CID=%%C"
    )

    :: Valid container ID has no spaces; docker error messages do
    if "!PA_CID!"=="" (
        call :log "  ERROR: docker create returned no output for !PA_IMG!"
        set /a G_FAIL+=1
        goto :eof
    )
    echo !PA_CID! | findstr " " >nul 2>&1
    if not errorlevel 1 (
        call :log "  ERROR: docker create failed: !PA_CID!"
        set "PA_CID="
        set /a G_FAIL+=1
        goto :eof
    )
    call :log "  Container: !PA_CID:~0,12!..."

    call :log "  Exporting filesystem (large images may take a minute)..."
    docker export "!PA_CID!" -o "!PA_TAR!" 2>&1
    if errorlevel 1 (
        call :log "  ERROR: docker export failed."
        docker rm -f "!PA_CID!" >nul 2>&1
        set /a G_FAIL+=1
        goto :eof
    )

    :: PowerShell scans tar entries and filters out system/framework paths
    call :log "  Scanning for JAR/WAR/EAR (ignoring system lib paths)..."
    set "PA_LIST=%TEMP%\mta_arts_%RANDOM%.txt"
    set "PSARG1=!PA_TAR!"
    set "PSARG2=!PA_LIST!"

    powershell -NoProfile -Command ^
        "$ign=@('WEB-INF/lib','BOOT-INF/lib','usr/lib/jvm','usr/java','jre/lib','jdk/lib','/.m2/','m2/repository','/.gradle/','var/cache/','usr/share/','usr/lib64/','modules/system/','wlp/lib/','tomcat/lib/','apache-tomcat/lib/','/node_modules/','/libs/','/lib/');" ^
        "$arts=(tar tf $env:PSARG1 2>$null)|Where-Object{$_ -match '\.(jar|war|ear)$'}|Where-Object{$e=$_;-not($ign|Where-Object{$e -match [regex]::Escape($_)})};" ^
        "if($arts){$arts|Set-Content -Encoding utf8 $env:PSARG2}" >nul 2>&1

    if exist "!PA_LIST!" (
        for /f "usebackq delims=" %%E in ("!PA_LIST!") do (
            set "PA_ENTRY=%%E"
            if not "!PA_ENTRY!"=="" (
                for %%N in ("!PA_ENTRY!") do set "PA_FNAME=%%~nxN"
                call :log "    Found: !PA_ENTRY!"

                tar xf "!PA_TAR!" -C "!PA_BDIR!" "!PA_ENTRY!" 2>nul

                :: Move file flat into PA_BDIR if tar nested it in subdirs
                if not exist "!PA_BDIR!\!PA_FNAME!" (
                    for /r "!PA_BDIR!" %%X in ("!PA_FNAME!") do (
                        if exist "%%X" move /y "%%X" "!PA_BDIR!\!PA_FNAME!" >nul 2>&1
                    )
                )

                if "!PA_FIRST!"=="" set "PA_FIRST=!PA_FNAME!"
                set /a PA_FOUND+=1
            )
        )
        del /q "!PA_LIST!" >nul 2>&1
    )

    :: Fallback: copy /app.jar directly from container
    if "!PA_FOUND!"=="0" (
        call :log "  Tar scan found nothing. Trying /app.jar fallback..."
        docker cp "!PA_CID!:/app.jar" "!PA_BDIR!\app.jar" >nul 2>&1
        if exist "!PA_BDIR!\app.jar" (
            call :log "  Fallback OK: /app.jar extracted."
            set "PA_FIRST=app.jar"
            set "PA_FOUND=1"
        )
    )

    if "!PA_FOUND!"=="0" (
        call :log "  ERROR: No JAR/WAR/EAR found in !PA_IMG!"
        call :log "  TIP: docker run --rm !PA_IMG! find / -name *.jar 2>/dev/null"
        call :cleanup "!PA_CID!" "!PA_IMG!" "!PA_WDIR!"
        set /a G_FAIL+=1
        goto :eof
    )
    call :log "  Found !PA_FOUND! artifact(s). Primary: !PA_FIRST!"

    del /q "!PA_TAR!" >nul 2>&1

    :: ============================================================
    ::  [2/5]  Create MTA Application -> get app_id
    :: ============================================================
    call :log "[2/5] Creating MTA Application..."

    :: Idempotency: delete existing app with same name
    set "PA_APPS=%TEMP%\mta_apps_%RANDOM%.json"
    curl -sk --max-time 30 -o "!PA_APPS!" "!MTA_URL!/hub/applications" >nul 2>&1

    set "PA_OLD_ID="
    if exist "!PA_APPS!" (
        set "PSARG1=!PA_APPS!"
        set "PSARG2=!PA_APP!"
        for /f "delims=" %%I in ('powershell -NoProfile -Command ^
            "try{$j=Get-Content -Raw $env:PSARG1|ConvertFrom-Json;($j|Where-Object{$_.name -eq $env:PSARG2}|Select-Object -First 1).id}catch{}" 2^>nul') do (
            set "PA_OLD_ID=%%I"
        )
        del /q "!PA_APPS!" >nul 2>&1
    )

    if not "!PA_OLD_ID!"=="" (
        call :log "  Removing existing app ID=!PA_OLD_ID! (idempotency)..."
        curl -sk --max-time 30 -X DELETE "!MTA_URL!/hub/applications/!PA_OLD_ID!" >nul 2>&1
        timeout /t 2 /nobreak >nul 2>&1
    ) else (
        call :log "  No existing app found."
    )

    :: POST /hub/applications
    set "PA_BFILE=%TEMP%\mta_body_%RANDOM%.json"
    set "PA_CRSP=%TEMP%\mta_create_%RANDOM%.json"
    > "!PA_BFILE!" echo {"name":"!PA_APP!","description":"Image: !PA_IMG!"}
    curl -sk --max-time 30 -X POST ^
        -H "Content-Type: application/json" ^
        -d "@!PA_BFILE!" ^
        -o "!PA_CRSP!" ^
        "!MTA_URL!/hub/applications" >nul 2>&1
    del /q "!PA_BFILE!" >nul 2>&1

    set "PA_APP_ID="
    if exist "!PA_CRSP!" (
        set "PSARG1=!PA_CRSP!"
        for /f "delims=" %%I in ('powershell -NoProfile -Command ^
            "try{(Get-Content -Raw $env:PSARG1|ConvertFrom-Json).id}catch{}" 2^>nul') do (
            set "PA_APP_ID=%%I"
        )
        if "!PA_APP_ID!"=="" (
            set /p _ERR=<"!PA_CRSP!"
            call :log "  ERROR: App creation failed. Server: !_ERR!"
        )
        del /q "!PA_CRSP!" >nul 2>&1
    )

    if "!PA_APP_ID!"=="" (
        call :cleanup "!PA_CID!" "!PA_IMG!" "!PA_WDIR!"
        set /a G_FAIL+=1
        goto :eof
    )
    call :log "  Application created: ID=!PA_APP_ID!"

    :: ============================================================
    ::  [3/5]  Create Analysis Task (state=Created, NOT started)
    ::         -> get task_id
    ::
    ::  PA_FIRST is already known from Step 1, so we embed the
    ::  artifact path in the task JSON at creation time.
    ::  MTA bucket path:  /binary/{filename}
    ::  Upload target  :  /hub/tasks/{id}/bucket/binary/{filename}
    :: ============================================================
    call :log "[3/5] Creating Analysis Task (state=Created)..."
    set "PA_ARTIFACT=/binary/!PA_FIRST!"
    call :log "  Artifact path in task: !PA_ARTIFACT!"

    :: Build targets JSON array with a CMD for loop.
    :: (ConvertTo-Json on a single item returns a bare string, not array)
    set "PA_TARR=["
    set "_TFIRST=1"
    for %%T in ("!TARGETS:,=" "!") do (
        set "_TV=%%~T"
        for /f "tokens=* delims= " %%S in ("!_TV!") do set "_TV=%%S"
        if not "!_TV!"=="" (
            if "!_TFIRST!"=="1" (
                set "PA_TARR=!PA_TARR!""!_TV!"""
                set "_TFIRST=0"
            ) else (
                set "PA_TARR=!PA_TARR!,""!_TV!"""
            )
        )
    )
    set "PA_TARR=!PA_TARR!]"

    set "PA_TFILE=%TEMP%\mta_task_%RANDOM%.json"
    set "PA_TRSP=%TEMP%\mta_taskr_%RANDOM%.json"

    (
        echo {
        echo   "name": "!PA_APP!-analysis",
        echo   "state": "Created",
        echo   "addon": "analyzer",
        echo   "application": {"id": !PA_APP_ID!},
        echo   "data": {
        echo     "mode": {
        echo       "artifact": "!PA_ARTIFACT!",
        echo       "binary": true,
        echo       "withDeps": false,
        echo       "diva": false
        echo     },
        echo     "targets": !PA_TARR!,
        echo     "sources": [],
        echo     "scope": {"withKnown": false},
        echo     "rules": {"path": "", "tags": []}
        echo   }
        echo }
    ) > "!PA_TFILE!"

    curl -sk --max-time 60 -X POST ^
        -H "Content-Type: application/json" ^
        -d "@!PA_TFILE!" ^
        -o "!PA_TRSP!" ^
        "!MTA_URL!/hub/tasks" >nul 2>&1
    del /q "!PA_TFILE!" >nul 2>&1

    set "PA_TASK_ID="
    if exist "!PA_TRSP!" (
        set "PSARG1=!PA_TRSP!"
        for /f "delims=" %%I in ('powershell -NoProfile -Command ^
            "try{(Get-Content -Raw $env:PSARG1|ConvertFrom-Json).id}catch{}" 2^>nul') do (
            set "PA_TASK_ID=%%I"
        )
        if "!PA_TASK_ID!"=="" (
            set /p _TERR=<"!PA_TRSP!"
            call :log "  ERROR: Task creation failed. Server: !_TERR!"
        )
        del /q "!PA_TRSP!" >nul 2>&1
    )

    if "!PA_TASK_ID!"=="" (
        call :log "  ERROR: Could not get task ID. Aborting !PA_APP!."
        curl -sk --max-time 30 -X DELETE "!MTA_URL!/hub/applications/!PA_APP_ID!" >nul 2>&1
        call :cleanup "!PA_CID!" "!PA_IMG!" "!PA_WDIR!"
        set /a G_FAIL+=1
        goto :eof
    )
    call :log "  Task created: ID=!PA_TASK_ID! (state=Created)"

    :: ============================================================
    ::  [4/5]  Upload binary to task bucket
    ::         PUT /hub/tasks/{task_id}/bucket/binary/{filename}
    :: ============================================================
    call :log "[4/5] Uploading binaries to task bucket (Task ID=!PA_TASK_ID!)..."
    set "PA_BASE=!MTA_URL!/hub/tasks/!PA_TASK_ID!/bucket/binary"

    for %%X in (jar war ear) do (
        for %%B in ("!PA_BDIR!\*.%%X") do (
            if exist "%%~fB" (
                set "PA_BNAME=%%~nxB"
                set "PA_BPATH=%%~fB"
                call :log "  -> !PA_BNAME!"
                call :log "     PUT !PA_BASE!/!PA_BNAME!"

                curl -sk --max-time 300 -X PUT ^
                    -H "Content-Type: application/octet-stream" ^
                    --data-binary "@!PA_BPATH!" ^
                    "!PA_BASE!/!PA_BNAME!" >nul 2>&1

                if errorlevel 1 (
                    call :log "     WARNING: curl returned non-zero for !PA_BNAME!"
                ) else (
                    call :log "     OK: !PA_BNAME! uploaded."
                    set /a PA_UPL+=1
                    if "!PA_PRIM!"=="" set "PA_PRIM=!PA_BNAME!"
                )
            )
        )
    )

    if "!PA_UPL!"=="0" (
        call :log "  ERROR: No files uploaded for !PA_APP!. Cleaning up..."
        curl -sk --max-time 30 -X DELETE "!MTA_URL!/hub/tasks/!PA_TASK_ID!" >nul 2>&1
        curl -sk --max-time 30 -X DELETE "!MTA_URL!/hub/applications/!PA_APP_ID!" >nul 2>&1
        call :cleanup "!PA_CID!" "!PA_IMG!" "!PA_WDIR!"
        set /a G_FAIL+=1
        goto :eof
    )
    call :log "  !PA_UPL! file(s) uploaded to task bucket."

    :: ============================================================
    ::  [5/5]  Submit task -> PUT /hub/tasks/{task_id}/submit
    ::         state=Ready triggers the MTA analysis engine
    :: ============================================================
    call :log "[5/5] Submitting task ID=!PA_TASK_ID!..."

    set "PA_SFILE=%TEMP%\mta_sub_%RANDOM%.json"
    set "PA_SRSP=%TEMP%\mta_subr_%RANDOM%.json"
    > "!PA_SFILE!" echo {"state":"Ready"}

    curl -sk --max-time 30 -X PUT ^
        -H "Content-Type: application/json" ^
        -d "@!PA_SFILE!" ^
        -o "!PA_SRSP!" ^
        "!MTA_URL!/hub/tasks/!PA_TASK_ID!/submit" >nul 2>&1

    :: Check errorlevel BEFORE del — del resets errorlevel to 0
    :: so checking after del would always show success
    set "PA_SOK=1"
    if errorlevel 1 (
        call :log "  WARNING: curl returned non-zero on submit."
        set "PA_SOK=0"
    )
    del /q "!PA_SFILE!" >nul 2>&1
    if exist "!PA_SRSP!" (
        findstr /i "error" "!PA_SRSP!" >nul 2>&1
        if not errorlevel 1 (
            set /p _SERR=<"!PA_SRSP!"
            call :log "  WARNING: Submit response contains error: !_SERR!"
            set "PA_SOK=0"
        )
        del /q "!PA_SRSP!" >nul 2>&1
    )

    if "!PA_SOK!"=="0" (
        call :log "  ERROR: Task submit failed for !PA_APP!."
        call :cleanup "!PA_CID!" "!PA_IMG!" "!PA_WDIR!"
        set /a G_FAIL+=1
        goto :eof
    )

    call :log "  Task submitted: ID=!PA_TASK_ID! -> state=Ready (queued)"
    call :log "SUCCESS: !PA_APP! | AppID=!PA_APP_ID! | TaskID=!PA_TASK_ID! | Files=!PA_UPL!"

    call :cleanup "!PA_CID!" "!PA_IMG!" "!PA_WDIR!"
    set /a G_SUCCESS+=1
    goto :eof


:: ==================================================================
::  SUBROUTINE  cleanup  "CONTAINER_ID" "IMAGE" "WORK_DIR"
:: ==================================================================
:cleanup
    if not "%~1"=="" docker rm -f "%~1" >nul 2>&1
    if "%KEEP_ARTIFACTS%"=="0" (
        if not "%~2"=="" docker rmi -f "%~2" >nul 2>&1
        if exist "%~3" rd /s /q "%~3" >nul 2>&1
    )
    goto :eof


:: ==================================================================
::  SUBROUTINE  trimvar  "VARIABLE_NAME"
:: ==================================================================
:trimvar
    set "VAR_NAME=%~1"
    set "VALUE=!%VAR_NAME%!"
    for /f "tokens=* delims= " %%T in ("!VALUE!") do set "VALUE=%%T"
:trimvar_loop
    if "!VALUE:~-1!"==" " (
        set "VALUE=!VALUE:~0,-1!"
        goto trimvar_loop
    )
    set "%VAR_NAME%=!VALUE!"
    goto :eof


:: ==================================================================
::  SUBROUTINE  log  "MESSAGE"
:: ==================================================================
:log
    echo [%date% %time%] %~1
    echo [%date% %time%] %~1>> "!LOG_FILE!"
    goto :eof
