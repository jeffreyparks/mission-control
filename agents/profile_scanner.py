"""
GitHub Profile Scanner
Analyzes GitHub activity against career goals
"""
import requests
import json
from datetime import datetime, timedelta
from pathlib import Path
from collections import Counter

class GitHubScanner:
    def __init__(self, username, base_dir):
        self.username = username
        self.base_dir = Path(base_dir)
        self.api_base = "https://api.github.com"
        
    def _get(self, endpoint):
        """Make GitHub API request (anonymous)"""
        url = f"{self.api_base}/{endpoint}"
        headers = {"Accept": "application/vnd.github.v3+json"}
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        return response.json()
    
    def fetch_user_profile(self):
        """Get basic user info"""
        return self._get(f"users/{self.username}")
    
    def fetch_repos(self):
        """Get user's public repos"""
        return self._get(f"users/{self.username}/repos?sort=updated&per_page=100")
    
    def fetch_recent_events(self):
        """Get recent public activity"""
        return self._get(f"users/{self.username}/events/public?per_page=100")
    
    def analyze_repos(self, repos):
        """Analyze repo characteristics"""
        languages = []
        topics = []
        total_stars = 0
        
        for repo in repos:
            if repo.get("language"):
                languages.append(repo["language"])
            total_stars += repo.get("stargazers_count", 0)
            topics.extend(repo.get("topics", []))
        
        return {
            "total_repos": len(repos),
            "languages": dict(Counter(languages).most_common()),
            "topics": dict(Counter(topics).most_common(10)),
            "total_stars": total_stars,
            "recently_updated": [
                {
                    "name": r["name"],
                    "updated": r["updated_at"],
                    "language": r.get("language", "Unknown"),
                    "stars": r.get("stargazers_count", 0)
                }
                for r in sorted(repos, key=lambda x: x["updated_at"], reverse=True)[:5]
            ]
        }
    
    def analyze_activity(self, events):
        """Analyze recent activity"""
        week_ago = datetime.now() - timedelta(days=7)
        event_types = []
        repos_touched = set()
        
        for event in events:
            event_date = datetime.strptime(event["created_at"], "%Y-%m-%dT%H:%M:%SZ")
            if event_date >= week_ago:
                event_types.append(event["type"])
                repos_touched.add(event["repo"]["name"])
        
        return {
            "events_last_7_days": len(event_types),
            "event_breakdown": dict(Counter(event_types)),
            "repos_active": list(repos_touched)
        }
    
    def load_career_goals(self):
        """Parse career goals from config"""
        goals_path = self.base_dir / "config/career-goals.md"
        if not goals_path.exists():
            return {"keywords": []}
        
        content = goals_path.read_text()
        
        # Simple keyword extraction from the Keywords section
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
    
    def check_coverage(self, analysis, goals):
        """Check alignment between activity and career goals"""
        keywords = set(goals.get("keywords", []))
        if not keywords:
            return {"status": "No keywords defined in career-goals.md"}
        
        # Check languages
        languages_str = " ".join(analysis["languages"].keys()).lower()
        
        # Check topics
        topics_str = " ".join(analysis["topics"].keys()).lower()
        
        # Combined searchable text
        profile_text = f"{languages_str} {topics_str}"
        
        matches = [kw for kw in keywords if kw in profile_text]
        missing = [kw for kw in keywords if kw not in profile_text]
        
        return {
            "keywords_matched": matches,
            "keywords_missing": missing,
            "coverage_pct": round(len(matches) / len(keywords) * 100) if keywords else 0
        }
    
    def generate_report(self, profile, repo_analysis, activity, coverage):
        """Generate markdown report"""
        today = datetime.now().strftime("%Y-%m-%d")
        
        report = f"""# GitHub Profile Scan: {self.username}
**Date:** {today}
**Profile:** https://github.com/{self.username}

## Profile Summary
- **Name:** {profile.get('name', 'N/A')}
- **Bio:** {profile.get('bio', 'N/A')}
- **Public Repos:** {profile.get('public_repos', 0)}
- **Followers:** {profile.get('followers', 0)}

## Recent Activity (Last 7 Days)
- **Total Events:** {activity['events_last_7_days']}
- **Active Repos:** {len(activity['repos_active'])}

### Event Breakdown
"""
        for event_type, count in activity['event_breakdown'].items():
            report += f"- {event_type}: {count}\n"
        
        report += f"""
## Repository Analysis
- **Total Public Repos:** {repo_analysis['total_repos']}
- **Total Stars:** {repo_analysis['total_stars']}

### Languages Used
"""
        for lang, count in list(repo_analysis['languages'].items())[:5]:
            report += f"- {lang}: {count} repos\n"
        
        report += "\n### Top Topics\n"
        if repo_analysis['topics']:
            for topic, count in list(repo_analysis['topics'].items())[:5]:
                report += f"- {topic}: {count} repos\n"
        else:
            report += "*No topics found*\n"
        
        report += "\n### Recently Updated Repos\n"
        for repo in repo_analysis['recently_updated']:
            report += f"- **{repo['name']}** ({repo['language']}) - ⭐ {repo['stars']} - Updated: {repo['updated'][:10]}\n"
        
        report += f"""
## Career Goals Alignment

### Keyword Coverage: {coverage['coverage_pct']}%

**Matched Keywords:**
"""
        if coverage['keywords_matched']:
            for kw in coverage['keywords_matched']:
                report += f"- ✓ {kw}\n"
        else:
            report += "*None*\n"
        
        report += "\n**Missing Keywords:**\n"
        if coverage['keywords_missing']:
            for kw in coverage['keywords_missing']:
                report += f"- ✗ {kw}\n"
        else:
            report += "*None - full coverage!*\n"
        
        report += f"""
## Recommendations
"""
        if coverage['coverage_pct'] < 50:
            report += "- 🔴 **Low keyword coverage** - Consider creating/contributing to repos in missing areas\n"
        elif coverage['coverage_pct'] < 80:
            report += "- 🟡 **Moderate coverage** - Good foundation, opportunity to expand into missing keywords\n"
        else:
            report += "- 🟢 **Strong coverage** - Profile aligns well with career goals\n"
        
        if activity['events_last_7_days'] == 0:
            report += "- 💡 Low recent activity - consider making a contribution this week\n"
        
        return report
    
    def run(self):
        """Execute the scan and generate report"""
        print(f"Scanning GitHub profile: {self.username}")
        
        # Fetch data
        profile = self.fetch_user_profile()
        repos = self.fetch_repos()
        events = self.fetch_recent_events()
        
        # Analyze
        repo_analysis = self.analyze_repos(repos)
        activity = self.analyze_activity(events)
        goals = self.load_career_goals()
        coverage = self.check_coverage(repo_analysis, goals)
        
        # Generate report
        report = self.generate_report(profile, repo_analysis, activity, coverage)
        
        # Save report
        today = datetime.now().strftime("%Y-%m-%d")
        output_path = self.base_dir / f"artifacts/profiles/scan-{today}.md"
        output_path.write_text(report)
        
        print(f"✓ Report saved to {output_path}")
        return output_path

if __name__ == "__main__":
    scanner = GitHubScanner("jeffreyparks", Path(__file__).parent.parent)
    scanner.run()
