"""
LinkedIn Profile Scanner
Parses LinkedIn data export and analyzes against career goals
"""
import zipfile
import csv
from pathlib import Path
from datetime import datetime
from collections import Counter
import json

class LinkedInScanner:
    def __init__(self, base_dir):
        self.base_dir = Path(base_dir)
        self.export_path = self.base_dir / "data/linkedin/linkedin-export.zip"
        self.extract_dir = self.base_dir / "data/linkedin/extracted"

    def check_pdf_exists(self):
        """Check if PDF profile exists"""
        pdf_path = self.base_dir / "data/linkedin/profile.pdf"
        return pdf_path.exists()
    
    def parse_pdf_profile(self):
        """Parse LinkedIn PDF profile export"""
        from pypdf import PdfReader
        
        pdf_path = self.base_dir / "data/linkedin/profile.pdf"
        reader = PdfReader(pdf_path)
        
        # Extract all text
        full_text = ""
        for page in reader.pages:
            full_text += page.extract_text()
        
        # Simple parsing - extract sections
        lines = full_text.split("\n")
        
        # Try to find key sections
        profile = {
            "name": "",
            "headline": "",
            "summary": "",
            "full_text": full_text
        }
        
        # First few lines usually contain name and headline
        if len(lines) > 0:
            profile["name"] = lines[0].strip()
        if len(lines) > 1:
            profile["headline"] = lines[1].strip()
        
        # Extract skills if "Skills" section exists
        skills = []
        in_skills = False
        for line in lines:
            if "Skills" in line or "Competencies" in line:
                in_skills = True
                continue
            if in_skills:
                # Skills usually end at next major section
                if any(section in line for section in ["Experience", "Education", "Certifications", "Languages"]):
                    break
                if line.strip() and len(line.strip()) > 2:
                    skills.append(line.strip())
        
        return profile, skills, full_text
    
    def analyze_pdf_keywords(self, full_text):
        """Check keyword coverage in PDF text"""
        goals = self.load_career_goals()
        keywords = set(goals.get("keywords", []))
        
        if not keywords:
            return {"status": "No keywords defined"}
        
        text_lower = full_text.lower()
        
        matches = [kw for kw in keywords if kw in text_lower]
        missing = [kw for kw in keywords if kw not in text_lower]
        
        return {
            "keywords_matched": matches,
            "keywords_missing": missing,
            "coverage_pct": round(len(matches) / len(keywords) * 100) if keywords else 0
        }
    
    def generate_pdf_report(self, profile, skills, keyword_coverage):
        """Generate markdown report from PDF data"""
        today = datetime.now().strftime("%Y-%m-%d")
        
        report = f"""# LinkedIn Profile Scan (PDF)
**Date:** {today}
**Source:** Quick PDF export

## Profile Summary
- **Name:** {profile.get('name', 'N/A')}
- **Headline:** {profile.get('headline', 'N/A')}

## Skills Detected ({len(skills)} found)
"""
        for skill in skills[:15]:
            report += f"- {skill}\n"
        
        if len(skills) > 15:
            report += f"*... and {len(skills) - 15} more*\n"
        
        report += f"""
## Career Goals Alignment

### Keyword Coverage: {keyword_coverage['coverage_pct']}%

**Matched Keywords:**
"""
        if keyword_coverage['keywords_matched']:
            for kw in keyword_coverage['keywords_matched'][:20]:
                report += f"- ✓ {kw}\n"
            if len(keyword_coverage['keywords_matched']) > 20:
                report += f"*... and {len(keyword_coverage['keywords_matched']) - 20} more*\n"
        else:
            report += "*None*\n"
        
        report += "\n**Missing Keywords:**\n"
        if keyword_coverage['keywords_missing']:
            for kw in keyword_coverage['keywords_missing'][:20]:
                report += f"- ✗ {kw}\n"
            if len(keyword_coverage['keywords_missing']) > 20:
                report += f"*... and {len(keyword_coverage['keywords_missing']) - 20} more*\n"
        else:
            report += "*None - full coverage!*\n"
        
        report += f"""
## Recommendations
"""
        if keyword_coverage['coverage_pct'] < 50:
            report += "- 🔴 **Low keyword coverage** - Update headline, summary, or experience descriptions\n"
        elif keyword_coverage['coverage_pct'] < 80:
            report += "- 🟡 **Moderate coverage** - Consider adding missing keywords to visible sections\n"
        else:
            report += "- 🟢 **Strong coverage** - Profile aligns well with career goals\n"
        
        report += "\n---\n*Note: This is from the PDF export. For more detailed analysis (posts, engagement), add the full ZIP export.*\n"
        
        return report

        
    def check_export_exists(self):
        """Check if export file exists"""
        return self.export_path.exists()
    
    def extract_export(self):
        """Extract ZIP file"""
        if not self.check_export_exists():
            return False
            
        self.extract_dir.mkdir(exist_ok=True)
        
        with zipfile.ZipFile(self.export_path, 'r') as zip_ref:
            zip_ref.extractall(self.extract_dir)
        
        return True
    
    def read_csv_safe(self, filename):
        """Safely read CSV file, return empty list if not found"""
        csv_path = self.extract_dir / filename
        if not csv_path.exists():
            return []
        
        try:
            with open(csv_path, 'r', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                return list(reader)
        except Exception as e:
            print(f"Warning: Could not read {filename}: {e}")
            return []
    
    def parse_profile(self):
        """Parse Profile.csv"""
        profile_data = self.read_csv_safe("Profile.csv")
        if not profile_data:
            return {}
        
        profile = profile_data[0] if profile_data else {}
        return {
            "first_name": profile.get("First Name", ""),
            "last_name": profile.get("Last Name", ""),
            "headline": profile.get("Headline", ""),
            "summary": profile.get("Summary", ""),
            "industry": profile.get("Industry", ""),
            "location": profile.get("Geo Location", "")
        }
    
    def parse_positions(self):
        """Parse Positions.csv"""
        positions = self.read_csv_safe("Positions.csv")
        return [
            {
                "title": p.get("Title", ""),
                "company": p.get("Company Name", ""),
                "description": p.get("Description", ""),
                "started": p.get("Started On", ""),
                "finished": p.get("Finished On", "")
            }
            for p in positions
        ]
    
    def parse_skills(self):
        """Parse Skills.csv"""
        skills = self.read_csv_safe("Skills.csv")
        return [s.get("Name", "") for s in skills if s.get("Name")]
    
    def parse_posts(self):
        """Parse Posts.csv - your published content"""
        posts = self.read_csv_safe("Posts.csv")
        
        # Extract post data
        post_data = []
        for post in posts:
            post_data.append({
                "date": post.get("Date", ""),
                "text": post.get("ShareCommentary", "") or post.get("Content", ""),
                "link": post.get("SharedUrl", "")
            })
        
        return post_data
    
    def parse_reactions(self):
        """Parse Reactions.csv - engagement data"""
        reactions = self.read_csv_safe("Reactions.csv")
        return reactions
    
    def analyze_profile_keywords(self, profile, positions, skills):
        """Check keyword coverage in profile text"""
        goals = self.load_career_goals()
        keywords = set(goals.get("keywords", []))
        
        if not keywords:
            return {"status": "No keywords defined"}
        
        # Combine all profile text
        text_parts = [
            profile.get("headline", ""),
            profile.get("summary", ""),
            profile.get("industry", "")
        ]
        
        for pos in positions:
            text_parts.append(pos.get("title", ""))
            text_parts.append(pos.get("description", ""))
        
        text_parts.extend(skills)
        
        profile_text = " ".join(text_parts).lower()
        
        matches = [kw for kw in keywords if kw in profile_text]
        missing = [kw for kw in keywords if kw not in profile_text]
        
        return {
            "keywords_matched": matches,
            "keywords_missing": missing,
            "coverage_pct": round(len(matches) / len(keywords) * 100) if keywords else 0
        }
    
    def analyze_posting_activity(self, posts):
        """Analyze post frequency and topics"""
        if not posts:
            return {
                "total_posts": 0,
                "last_post_date": None,
                "post_frequency": "No posts found"
            }
        
        # Get date of most recent post
        dates = [p["date"] for p in posts if p["date"]]
        last_post = max(dates) if dates else None
        
        return {
            "total_posts": len(posts),
            "last_post_date": last_post,
            "recent_posts": posts[:5]  # Most recent 5
        }
    
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
    
    def generate_report(self, profile, positions, skills, posts_analysis, keyword_coverage):
        """Generate markdown report"""
        today = datetime.now().strftime("%Y-%m-%d")
        
        report = f"""# LinkedIn Profile Scan
**Date:** {today}

## Profile Summary
- **Name:** {profile.get('first_name', '')} {profile.get('last_name', '')}
- **Headline:** {profile.get('headline', 'N/A')}
- **Industry:** {profile.get('industry', 'N/A')}
- **Location:** {profile.get('location', 'N/A')}

## Current Position
"""
        if positions:
            current = positions[0]
            report += f"""- **{current['title']}** at {current['company']}
- Started: {current['started']}
"""
            if current['description']:
                report += f"- Description: {current['description'][:200]}...\n"
        else:
            report += "*No positions found*\n"
        
        report += f"""
## Skills ({len(skills)} total)
"""
        for skill in skills[:10]:
            report += f"- {skill}\n"
        
        if len(skills) > 10:
            report += f"*... and {len(skills) - 10} more*\n"
        
        report += f"""
## Content & Posting Activity
- **Total Posts:** {posts_analysis['total_posts']}
- **Last Post:** {posts_analysis.get('last_post_date', 'Unknown')}

"""
        if posts_analysis.get('recent_posts'):
            report += "### Recent Posts\n"
            for post in posts_analysis['recent_posts']:
                date = post['date'][:10] if post['date'] else 'Unknown'
                text = post['text'][:100] if post['text'] else '[No text]'
                report += f"- **{date}**: {text}...\n"
        
        report += f"""
## Career Goals Alignment

### Keyword Coverage: {keyword_coverage['coverage_pct']}%

**Matched Keywords:**
"""
        if keyword_coverage['keywords_matched']:
            for kw in keyword_coverage['keywords_matched']:
                report += f"- ✓ {kw}\n"
        else:
            report += "*None*\n"
        
        report += "\n**Missing Keywords:**\n"
        if keyword_coverage['keywords_missing']:
            for kw in keyword_coverage['keywords_missing']:
                report += f"- ✗ {kw}\n"
        else:
            report += "*None - full coverage!*\n"
        
        report += f"""
## Recommendations
"""
        if keyword_coverage['coverage_pct'] < 50:
            report += "- 🔴 **Low keyword coverage** - Update headline, summary, or skills to include missing terms\n"
        elif keyword_coverage['coverage_pct'] < 80:
            report += "- 🟡 **Moderate coverage** - Consider adding missing keywords to profile sections\n"
        else:
            report += "- 🟢 **Strong coverage** - Profile aligns well with career goals\n"
        
        if posts_analysis['total_posts'] == 0:
            report += "- 💡 No posts found - consider sharing insights or projects on LinkedIn\n"
        
        return report
    
    def run(self):
        """Execute the scan and generate report"""
        # Check for PDF first (quick export)
        if self.check_pdf_exists():
            print(f"Found PDF profile export, parsing...")
            try:
                profile, skills, full_text = self.parse_pdf_profile()
                keyword_coverage = self.analyze_pdf_keywords(full_text)
                report = self.generate_pdf_report(profile, skills, keyword_coverage)
                
                today = datetime.now().strftime("%Y-%m-%d")
                output_path = self.base_dir / f"artifacts/profiles/linkedin-scan-{today}.md"
                output_path.write_text(report)
                
                print(f"✓ Report saved to {output_path}")
                return output_path
            except Exception as e:
                print(f"⚠️  PDF parsing failed: {e}")
                print(f"   Falling back to ZIP export check...")
        
        # Fall back to ZIP export if no PDF or PDF failed
        if not self.check_export_exists():
            print(f"❌ LinkedIn export not found")
            print(f"   Drop profile.pdf OR linkedin-export.zip in: {self.base_dir}/data/linkedin/")
            print(f"   See: {self.base_dir}/data/linkedin/README.md for instructions")
            return None
        
        print(f"Extracting LinkedIn export...")
        self.extract_export()
        
        print("Parsing LinkedIn data...")
        profile = self.parse_profile()
        positions = self.parse_positions()
        skills = self.parse_skills()
        posts = self.parse_posts()
        
        print("Analyzing against career goals...")
        keyword_coverage = self.analyze_profile_keywords(profile, positions, skills)
        posts_analysis = self.analyze_posting_activity(posts)
        
        report = self.generate_report(profile, positions, skills, posts_analysis, keyword_coverage)
        
        # Save report
        today = datetime.now().strftime("%Y-%m-%d")
        output_path = self.base_dir / f"artifacts/profiles/linkedin-scan-{today}.md"
        output_path.write_text(report)
        
        print(f"✓ Report saved to {output_path}")
        return output_path


if __name__ == "__main__":
    scanner = LinkedInScanner(Path(__file__).parent.parent)
    scanner.run()
