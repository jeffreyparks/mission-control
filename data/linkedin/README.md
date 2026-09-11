# LinkedIn Data Export

## Quick Start: PDF Profile (Available Now)

**Fastest option - get your profile analyzed in 2 minutes:**

1. Go to: https://www.linkedin.com/in/jeff-parks-arjentic/
2. Click: "More" → "Save to PDF"
3. Save as: `profile.pdf`
4. Drop it here: `data/linkedin/profile.pdf`
5. Run: `uv run run_daily.py`

✓ Immediate results  
✓ Analyzes headline, summary, experience, skills  
✓ Keyword coverage vs career goals  

## Full Export: ZIP Archive (More Data, Takes 24 Hours)

**For complete analysis including posts, engagement, connections:**

1. Go to: https://www.linkedin.com/mypreferences/d/download-my-data
2. Select: **"Download larger data archive"** (not the fast export)
3. Click: "Request archive"
4. Wait for email (usually 10-30 minutes to 24 hours)
5. Download the ZIP file from the email link
6. Drop it here: `data/linkedin/linkedin-export.zip`

The scanner automatically uses ZIP data when available (more detailed than PDF).

## What Gets Analyzed

**PDF Export (Quick):**
- Profile headline & summary
- Experience descriptions
- Skills list
- Keyword coverage

**ZIP Export (Comprehensive):**
- All of the above, PLUS:
- Posts and articles you've published
- Engagement metrics
- Connection data
- More structured position history

## Privacy

All data stays on your local machine. Nothing is uploaded anywhere.

## File Locations

Drop your export(s) here:
```
data/linkedin/profile.pdf           ← Quick PDF export (use now)
data/linkedin/linkedin-export.zip   ← Full export (when available)
```

The scanner will automatically use whichever you have available, preferring PDF for speed.
