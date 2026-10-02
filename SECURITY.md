# Security and privacy

## Data handled by the application

- Space URLs entered by the user
- Public Space metadata and audio obtained from X
- When explicitly selected, X cookies read locally from the chosen browser by yt-dlp

The application has no analytics, advertising, telemetry, update service, or developer-operated server. It does not ask for an X username or password. Browser cookies are not written into the recording folder or included in diagnostic files by this application.

UI preferences are stored locally in `%LOCALAPPDATA%\X Space Recorder\settings.json`. Only the output folder, audio format, browser choice and light/dark theme are stored; Space URLs, cookies and passwords are not. These settings are not included in release ZIPs.

Recordings remain only in the folder selected by the user. Temporary audio chunks are removed after a successful save; if recording fails, a hidden `.recording-*` recovery folder may remain inside the selected recording folder.

Conversion takes place inside that temporary folder. A completed output file is published only after successful conversion, without overwriting existing recordings. Errors stop and join the session's background workers before the UI permits another recording.

## Reporting a security problem

Do not post cookies, passwords, private Space URLs, or personal recordings in a public issue. Describe the problem without sensitive data.
