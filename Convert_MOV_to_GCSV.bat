@echo off
setlocal
if "%~1"=="" (
  echo Drag one or more MotionCam MOV files onto this file.
  echo Requires Python 3 and FFmpeg with ffprobe on PATH.
  pause
  exit /b 1
)
set "PYTHON_CMD=py -3"
where py >nul 2>nul || set "PYTHON_CMD=python"
:next
if "%~1"=="" goto done
if /I "%~x1"==".mov" (
  %PYTHON_CMD% "%~dp0repair_motioncam_gpmf.py" "%~1"
) else (
  echo Skipping non-MOV file: %~1
)
shift
goto next
:done
echo.
echo Finished. Review the results in Gyroflow before using them.
pause
