# Canvas Scraper

## Setup
Add your institution's URL and your personal canvas API token in .env.example then run
```
cp .env.example.env
```
## Start Scraping
```
venv/bin/python canvas_export.py <course_id>
```
**NOTE!**

Course ID is at the end of the URL of your course, **NOT** the course code from your institution.


