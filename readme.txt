Purpose
-------
Cricket data and admin stack: backend API, cricket_ui front end, infra, packaged tarballs for deploy, logs, and optional disk analyzer exports.

How to use
----------
1. Start from backend/ and cricket_ui/ README or package files if present: install dependencies, configure environment for your database and auth.
2. Use infra/ for deployment or IaC scripts tied to this project.
3. cricket_ui_deploy_*.tar.gz archives are deployment bundles; unpack on the server or compare with a fresh build before promoting.
4. disk_analyzer_* files are side artifacts from disk usage tooling; safe to regenerate or ignore for the core app.
