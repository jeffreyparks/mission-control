"""
Org/Roles Scanner Agent
Scans job boards and company career pages for relevant roles
"""
import requests
import yaml
import pandas as pd
from datetime import datetime, timedelta
from job_fetch import _html_to_text
from builtin_source import fetch_builtin_jobs as _fetch_builtin_jobs
from jobspy_source import fetch_jobspy_jobs as _fetch_jobspy_jobs
from profile_keywords import load_keywords, matches_exclude
from pathlib import Path
import re


def _config_path(base_dir, filename):
    """Per-workspace config when present, else the repo's tracked default, so a
    new workspace works with no setup but can still override."""
    from pathlib import Path as _P
    local = _P(base_dir) / "config" / filename
    if local.exists():
        return local
    return _P(__file__).resolve().parent.parent / "config" / filename

class JobScanner:
    def __init__(self, base_dir):
        self.base_dir = Path(base_dir)
        self.sources_path = _config_path(self.base_dir, "job-sources.yaml")
        
    def load_sources(self):
        """Load job sources from YAML config"""
        if not self.sources_path.exists():
            print(f"⚠️  No job sources found at {self.sources_path}")
            return {}
        
        with open(self.sources_path, 'r') as f:
            return yaml.safe_load(f)
    
    def load_career_goals(self):
        """Keyword lists from me/profile.md - the single place keywords live.

        `keywords` (## Target Keywords) raise a role's score and generate the
        JobSpy board queries; `exclude` (## Exclude Keywords) kill a role
        outright. See agents/profile_keywords.py.
        """
        lists = load_keywords(self.base_dir)
        return {"keywords": lists["target"], "exclude": lists["exclude"]}
    
    def load_existing_tracker(self):
        """Existing roles, from the database - the only record."""
        from store import Store

        store = Store(self.base_dir)
        if store.is_empty():
            return pd.DataFrame()
        return store.load(verbose=False)
    
    def fetch_greenhouse_jobs(self, company_name, api_url):
        """Fetch jobs from Greenhouse API"""
        try:
            response = requests.get(api_url, timeout=10)
            response.raise_for_status()
            data = response.json()
            
            jobs = []
            for job in data.get('jobs', []):
                jobs.append({
                    'company': company_name,
                    'title': job.get('title', ''),
                    'url': job.get('absolute_url', ''),
                    'location': job.get('location', {}).get('name', ''),
                    'description': f"{job.get('title', '')} {job.get('content', '')}",
                    'posted': job.get('updated_at', '')
                })
            
            return jobs
        except Exception as e:
            print(f"  ✗ {company_name}: {e}")
            return []
    
    def fetch_lever_jobs(self, company_name, api_url):
        """Fetch jobs from Lever API"""
        try:
            response = requests.get(api_url, timeout=10)
            response.raise_for_status()
            data = response.json()
            
            jobs = []
            for job in data:
                jobs.append({
                    'company': company_name,
                    'title': job.get('text', ''),
                    'url': job.get('hostedUrl', ''),
                    'location': job.get('categories', {}).get('location', ''),
                    'description': f"{job.get('text', '')} {job.get('description', '')}",
                    'posted': job.get('createdAt', '')
                })
            
            return jobs
        except Exception as e:
            print(f"  ✗ {company_name}: {e}")
            return []
    
    def fetch_ashby_jobs(self, company_name, api_url):
        """Fetch jobs from an Ashby job-board listing API.

        api_url is the public board endpoint, e.g.
        https://api.ashbyhq.com/posting-api/job-board/<org-slug>
        - no auth, same shape job_fetch.py already reads for a single posting.
        """
        try:
            response = requests.get(api_url, timeout=10)
            response.raise_for_status()
            data = response.json()

            jobs = []
            for job in data.get('jobs', []):
                if not job.get('isListed', True):
                    continue
                location = job.get('location') or (
                    job.get('address', {}).get('postalAddress', {}).get('addressLocality')
                )
                jobs.append({
                    'company': company_name,
                    'title': job.get('title', ''),
                    'url': job.get('jobUrl', ''),
                    'location': location or '',
                    'description': f"{job.get('title', '')} {_html_to_text(job.get('descriptionHtml')) or ''}",
                    'posted': job.get('publishedAt', ''),
                })

            return jobs
        except Exception as e:
            print(f"  ✗ {company_name}: {e}")
            return []

    def fetch_builtin_jobs(self, label, agg_config):
        """Board-wide Built In aggregator scan.

        Queries are derived from me/profile.md's Target Keywords, not from
        job-sources.yaml, so keywords stay in ONE place. Built In has no
        negative-term syntax, so Exclude Keywords cannot be pushed into the
        query; they are enforced on the title by the live-scan filter instead.
        See agents/builtin_source.py.
        """
        lists = load_keywords(self.base_dir)
        max_queries = int(agg_config.get("max_queries") or 8)
        return _fetch_builtin_jobs(
            queries=lists["target"][:max_queries],
            categories=agg_config.get("categories"),
            host=agg_config.get("host"),
            scope=agg_config.get("scope"),
            max_pages=agg_config.get("max_pages", 3),
            label=label,
        )

    def fetch_jobspy_jobs(self, label, agg_config):
        """Multi-board scan (Indeed, LinkedIn, ...) via python-jobspy.

        Queries are derived from me/profile.md, not from job-sources.yaml, so
        keywords stay in one place. See agents/jobspy_source.py.
        """
        lists = load_keywords(self.base_dir)
        return _fetch_jobspy_jobs(
            target_keywords=lists["target"],
            exclude_keywords=lists["exclude"],
            agg_config=agg_config,
            label=label,
        )

    def calculate_match_score(self, job_text, keywords, excludes, rules, job_title=""):
        """Score a role against the profile's Target Keywords.

        Target keywords BOOST a score; they are not a gate, and they match
        anywhere in the TITLE or the DESCRIPTION - a role titled for the work
        you want should score for it even when the description is vague.

        The only hard filters are the exclude list and rules.min_match_score,
        so lowering min_match_score to 0 lets everything through to the LLM
        fit read.
        """
        job_lower = f"{job_title or ''} {job_text or ''}".lower()

        # Exclusions are absolute - one match on the TITLE and the role is gone.
        # TITLE ONLY, with no fallback to the description: matching the whole
        # description would close any role that merely mentions "mentoring
        # interns" in a bullet, and an untitled posting must not be judged on
        # its description either.
        if matches_exclude(job_title, excludes):
            return 0, []

        matches = [kw for kw in keywords if kw in job_lower]
        if not matches:
            return 0, []

        # First match carries the role; each further match adds confidence.
        return min(40 + (len(matches) * 10), 100), matches
    
    def is_duplicate(self, job, existing_df):
        """Check if job already exists in tracker"""
        if existing_df.empty:
            return False
        
        # Check for matching URL
        if job['url'] and job['url'] in existing_df['Role Link'].values:
            return True
        
        # Check for matching company + title
        matches = existing_df[
            (existing_df['Org'].str.lower() == job['company'].lower()) &
            (existing_df['Title'].str.lower() == job['title'].lower())
        ]
        
        return len(matches) > 0
    
    def scan_companies(self, config, goals, rules):
        """Scan all configured companies"""
        companies = config.get('companies', [])
        keywords = goals.get('keywords', [])
        excludes = goals.get('exclude', [])
        
        all_jobs = []
        
        print(f"Scanning {len(companies)} companies...")
        print(f"Tracking {len(keywords)} keywords")
        
        for company in companies:
            name = company['name']
            api_url = company.get('api_url')
            
            if not api_url:
                print(f"  ⊘ {name}: No API URL configured")
                continue
            
            print(f"  Fetching: {name}...")
            
            # Determine API type and fetch
            jobs = []
            if 'greenhouse' in api_url:
                jobs = self.fetch_greenhouse_jobs(name, api_url)
            elif 'lever' in api_url:
                jobs = self.fetch_lever_jobs(name, api_url)
            elif 'ashbyhq' in api_url:
                jobs = self.fetch_ashby_jobs(name, api_url)
            
            if not jobs:
                continue
            
            # Score and filter jobs
            relevant_jobs = []
            for job in jobs:
                score, matches = self.calculate_match_score(
                    job['description'],
                    keywords,
                    excludes,
                    rules,
                    job.get('title', '')
                )
                
                if score >= rules.get('min_match_score', 30):
                    job['match_score'] = score
                    job['keywords_matched'] = ', '.join(matches[:5])  # Top 5
                    job['priority'] = company.get('priority', 3)
                    relevant_jobs.append(job)
            
            all_jobs.extend(relevant_jobs)
            print(f"    Found {len(relevant_jobs)} relevant roles")

        for agg in config.get('aggregators', []):
            provider = agg.get('provider')
            if provider == 'builtin':
                name = agg.get('name', 'Built In')
                fetch = self.fetch_builtin_jobs
            elif provider == 'jobspy':
                if agg.get('enabled') is False:
                    continue
                name = agg.get('name', 'JobSpy')
                fetch = self.fetch_jobspy_jobs
            else:
                continue
            print(f"  Fetching: {name}...")
            jobs = fetch(name, agg)
            relevant_jobs = []
            for job in jobs:
                score, matches = self.calculate_match_score(job['description'], keywords, excludes, rules,
                                                            job.get('title', ''))
                if score >= rules.get('min_match_score', 30):
                    job['match_score'] = score
                    job['keywords_matched'] = ', '.join(matches[:5])
                    job['priority'] = agg.get('priority', 3)
                    relevant_jobs.append(job)
            all_jobs.extend(relevant_jobs)
            print(f"    Found {len(relevant_jobs)} relevant roles")

        return all_jobs
    
    def update_tracker(self, new_jobs, existing_df):
        """Add new jobs to the tracker database."""
        if not new_jobs:
            print("⊘ No new jobs to add")
            return 0
        
        # Filter out duplicates
        unique_jobs = [j for j in new_jobs if not self.is_duplicate(j, existing_df)]
        
        if not unique_jobs:
            print("⊘ All jobs already in tracker")
            return 0
        
        # Convert to DataFrame rows
        new_rows = []
        for job in unique_jobs:
            new_rows.append({
                'Org': job['company'],
                'Title': job['title'],
                'Role Cat': '',  # User fills this
                'Priority': job.get('priority', 3),
                'Date Opened': datetime.now(),
                'Date Applied': pd.NaT,
                'Status': '01 Open',
                'Outcomes': '',
                'Source': 'Auto-scan',
                'Match Score': job['match_score'],
                'Keywords Matched': job['keywords_matched'],
                'Role Link': job['url'],
                'Range': '',
                'Notes': f"Auto-discovered. Location: {job.get('location', 'Unknown')}",
                'Other Links': '',
                'Last Updated': datetime.now()
            })
        
        # Append to existing data and persist to the database, which assigns
        # ids and logs every field it writes.
        new_df = pd.DataFrame(new_rows)
        combined = pd.concat([existing_df, new_df], ignore_index=True)

        from store import Store

        Store(self.base_dir).save_df(combined, actor="keyword-scan")
        print(f"✓ Added {len(new_rows)} new roles to tracker")

        return len(new_rows)
    
    def generate_report(self, new_jobs, added_count):
        """Generate daily scan report"""
        today = datetime.now().strftime("%Y-%m-%d")
        
        report = f"""# Job Scan Report
**Date:** {today}
**New Roles Found:** {len(new_jobs)}
**Added to Tracker:** {added_count}

## Summary

Scanned company career pages and found {len(new_jobs)} roles matching your career goals.
{added_count} new roles were added to the tracker (duplicates filtered).

"""
        
        if new_jobs:
            # Group by company
            by_company = {}
            for job in new_jobs:
                company = job['company']
                if company not in by_company:
                    by_company[company] = []
                by_company[company].append(job)
            
            report += "## New Roles by Company\n\n"
            for company, jobs in sorted(by_company.items()):
                report += f"### {company} ({len(jobs)} roles)\n\n"
                for job in sorted(jobs, key=lambda x: x['match_score'], reverse=True):
                    report += f"""**{job['title']}**
- Match Score: {job['match_score']}/100
- Keywords: {job['keywords_matched']}
- Location: {job.get('location', 'Unknown')}
- Link: {job['url']}

"""
        else:
            report += "*No new roles found matching your criteria.*\n\n"
        
        report += f"""
## Next Steps

1. Review new roles in the dashboard: `mc dashboard`
2. Fill in 'Role Cat' for new roles (01-06 from your career goals)
3. Update 'Priority' if needed
4. Update 'Status' as you research/apply

"""
        
        return report
    
    def run(self):
        """Execute job scan"""
        print("Running Org/Roles Scanner...")
        
        try:
            # Load config
            config = self.load_sources()
            goals = self.load_career_goals()
            rules = config.get('rules', {})
            
            # Load existing tracker
            existing_df = self.load_existing_tracker()
            print(f"✓ Loaded existing tracker: {len(existing_df)} roles")
            
            # Scan companies
            new_jobs = self.scan_companies(config, goals, rules)
            
            # Update tracker
            added_count = self.update_tracker(new_jobs, existing_df)
            
            # Generate report
            report = self.generate_report(new_jobs, added_count)
            
            # Save report
            today = datetime.now().strftime("%Y-%m-%d")
            output_path = self.base_dir / f"artifacts/jobs/scan-{today}.md"
            output_path.write_text(report)
            
            print(f"✓ Report saved to {output_path}")
            print(f"  New roles: {len(new_jobs)}")
            print(f"  Added to tracker: {added_count}")
            
            return output_path
            
        except Exception as e:
            print(f"✗ Job scan failed: {e}")
            import traceback
            traceback.print_exc()
            return None

if __name__ == "__main__":
    scanner = JobScanner(Path(__file__).parent.parent)
    scanner.run()
