"""
Org/Roles Scanner Agent
Scans job boards and company career pages for relevant roles
"""
import requests
import yaml
import pandas as pd
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote_plus
import re

class JobScanner:
    def __init__(self, base_dir):
        self.base_dir = Path(base_dir)
        self.sources_path = self.base_dir / "config/job-sources.yaml"
        self.tracker_path = self.base_dir / "artifacts/jobs/org-roles-tracker.xlsx"
        
    def load_sources(self):
        """Load job sources from YAML config"""
        if not self.sources_path.exists():
            print(f"⚠️  No job sources found at {self.sources_path}")
            return {}
        
        with open(self.sources_path, 'r') as f:
            return yaml.safe_load(f)
    
    def load_career_goals(self):
        """Parse career goals from config"""
        goals_path = self.base_dir / "config/career-goals.md"
        if not goals_path.exists():
            return {"keywords": []}
        
        content = goals_path.read_text()
        
        keywords = []
        in_keywords = False
        for line in content.split("\n"):
            if "## Keywords to Track" in line:
                in_keywords = True
                continue
            if in_keywords:
                if line.startswith("##"):
                    break
                if line.strip().startswith("-"):
                    keyword = line.strip().lstrip("-").strip().lower()
                    if keyword and not keyword.startswith("<!--"):
                        keywords.append(keyword)
        
        return {"keywords": keywords}
    
    def load_existing_tracker(self):
        """Load existing role tracker"""
        if not self.tracker_path.exists():
            return pd.DataFrame()
        
        return pd.read_excel(self.tracker_path)
    
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
    
    def calculate_match_score(self, job_text, keywords, rules):
        """Calculate how well a job matches career goals"""
        job_lower = job_text.lower()
        
        # Check exclusions first
        exclude_keywords = rules.get('exclude_keywords', [])
        for exclude in exclude_keywords:
            if exclude.lower() in job_lower:
                return 0, []
        
        # Count keyword matches
        matches = [kw for kw in keywords if kw in job_lower]
        
        if not matches:
            return 0, []
        
        # Base score from keyword count
        score = min(40 + (len(matches) * 10), 100)
        
        # Boost for must-have keywords
        must_have = rules.get('must_have_keywords', [])
        must_have_matches = [kw for kw in must_have if kw in job_lower]
        if must_have_matches:
            score = min(score + (len(must_have_matches) * 5), 100)
        
        return score, matches
    
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
    
    def generate_linkedin_urls(self, config):
        """Generate LinkedIn search URLs for keywords"""
        if not config.get('linkedin', {}).get('enabled'):
            return []
        
        linkedin_config = config['linkedin']
        keywords = linkedin_config.get('search_keywords', [])
        location = linkedin_config.get('location', '')
        
        urls = []
        for keyword in keywords:
            encoded_keyword = quote_plus(keyword)
            encoded_location = quote_plus(location)
            url = f"https://www.linkedin.com/jobs/search/?keywords={encoded_keyword}&location={encoded_location}"
            urls.append({
                'keyword': keyword,
                'url': url
            })
        
        return urls
    
    def scan_companies(self, config, goals, rules):
        """Scan all configured companies"""
        companies = config.get('companies', [])
        keywords = goals.get('keywords', [])
        
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
            
            if not jobs:
                continue
            
            # Score and filter jobs
            relevant_jobs = []
            for job in jobs:
                score, matches = self.calculate_match_score(
                    job['description'], 
                    keywords, 
                    rules
                )
                
                if score >= rules.get('min_match_score', 30):
                    job['match_score'] = score
                    job['keywords_matched'] = ', '.join(matches[:5])  # Top 5
                    job['priority'] = company.get('priority', 3)
                    relevant_jobs.append(job)
            
            all_jobs.extend(relevant_jobs)
            print(f"    Found {len(relevant_jobs)} relevant roles")
        
        return all_jobs
    
    def update_tracker(self, new_jobs, existing_df):
        """Add new jobs to tracker Excel file"""
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
        
        # Append to existing data
        new_df = pd.DataFrame(new_rows)
        combined = pd.concat([existing_df, new_df], ignore_index=True)
        
        # Save
        combined.to_excel(self.tracker_path, index=False)
        print(f"✓ Added {len(new_rows)} new roles to tracker")
        
        return len(new_rows)
    
    def generate_report(self, new_jobs, added_count, linkedin_urls):
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
        
        # LinkedIn search links
        if linkedin_urls:
            report += "## LinkedIn Search Links\n\n"
            report += "Click these to search LinkedIn for relevant roles:\n\n"
            for item in linkedin_urls:
                report += f"- [{item['keyword']}]({item['url']})\n"
        
        report += f"""
## Next Steps

1. Review new roles in: `artifacts/jobs/org-roles-tracker.xlsx`
2. Fill in 'Role Cat' for new roles (01-06 from your career goals)
3. Update 'Priority' if needed
4. Click LinkedIn links above to see aggregated results
5. Update 'Status' as you research/apply

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
            
            # Generate LinkedIn URLs
            linkedin_urls = self.generate_linkedin_urls(config)
            
            # Generate report
            report = self.generate_report(new_jobs, added_count, linkedin_urls)
            
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
