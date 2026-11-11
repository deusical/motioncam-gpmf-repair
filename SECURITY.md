# Security and privacy

This converter processes untrusted MOV inputs by invoking the installed `ffprobe` and `ffmpeg` executables without a shell, then parses GPMF metadata locally. Use maintained FFmpeg builds and do not run untrusted files with elevated privileges.

The program never sends footage or sensor data to a service; all conversions are local. Output GCSV files may nonetheless reveal identifying details about someone's movement and recording timestamps. Keep them private unless deliberately sharing them. The repository's `.gitignore` excludes video and gyro-log extensions, but Git can still force-add ignored files; inspect every staged change.

For a potential security vulnerability, contact the maintainer privately through GitHub rather than posting exploit details or sensitive video in a public issue. No formal vulnerability response SLA is offered for this experimental project.