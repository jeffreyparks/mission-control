"""
BlueSky Profile Scanner
Analyzes BlueSky activity via AT Protocol with custom PDS support
"""
import requests
from datetime import datetime, timedelta
from pathlib import Path
from collections import Counter
import re
import configparser

class BlueSkyScanner:
    def __init__(self, handle, base_dir):
        self.handle = handle
        self.base_dir = Path(base_dir)
        self.api_base = "https://bsky.social/xrpc"  # Default
        self.session = None
        
    def load_credentials(self):
        """Load BlueSky credentials from config file"""
        config_path = self.base_dir / "config/bluesky.conf"
        
        if not config_path.exists():
            return None, None, None
        
        config = configparser.ConfigParser()
        config.read(config_path)
        
        if 'bluesky' not in config:
            return None, None, None
        
        handle = config['bluesky'].get('handle', '').strip()
        password = config['bluesky'].get('app_password', '').strip()
        pds_url = config['bluesky'].get('pds_url', '').strip()
        
        if not handle or not password or password == 'YOUR_APP_PASSWORD_HERE':
            return None, None, None
        
        # Use custom PDS if provided, otherwise default to bsky.social
        if pds_url and pds_url != 'https://bsky.social':
            self.api_base = f"{pds_url}/xrpc"
            print(f"  Using custom PDS: {pds_url}")
        
        return handle, password, pds_url
    
    def authenticate(self):
        """Create authenticated session"""
        handle, password, pds_url = self.load_credentials()
        
        if not handle or not password:
            print("⚠️  No credentials found in config/bluesky.conf")
            return False
        
        # Create session (use the correct base URL set in load_credentials)
        url = f"{self.api_base}/com.atproto.server.createSession"
        data = {
            "identifier": handle,
            "password": password
        }
        
        response = requests.post(url, json=data)
        response.raise_for_status()
        
        self.session = response.json()
        return True
    
    def _get(self, endpoint, params=None):
        """Make authenticated AT Protocol API request"""
        url = f"{self.api_base}/{endpoint}"
        headers = {}
        
        if self.session:
            headers["Authorization"] = f"Bearer {self.session['accessJwt']}"
        
        response = requests.get(url, params=params, headers=headers)
        response.raise_for_status()
        return response.json()
    
    def fetch_profile(self):
        """Get profile information"""
        return self._get("app.bsky.actor.getProfile", {"actor": self.handle})
    
    def fetch_posts(self, limit=50):
        """Get recent posts from author feed"""
        return self._get("app.bsky.feed.getAuthorFeed", {
            "actor": self.handle,
            "limit": limit
        })
    
    def analyze_posts(self, feed_data):
        """Analyze post content and engagement"""
        posts = feed_data.get("feed", [])
        
        if not posts:
            return {
                "total_posts": 0,
                "posts_last_30_days": 0,
                "total_likes": 0,
                "total_reposts": 0,
                "hashtags": {},
                "recent_posts": [],
                "all_text": ""
            }
        
        from datetime import timezone
        thirty_days_ago = datetime.now(timezone.utc) - timedelta(days=30)
        seven_days_ago = datetime.now(timezone.utc) - timedelta(days=7)
        
        total_likes = 0
        total_reposts = 0
        posts_last_30 = 0
        posts_last_7 = 0
        hashtags = []
        recent_posts = []
        all_text = []
        
        for item in posts:
            post = item.get("post", {})
            record = post.get("record", {})
            
            # Get post text
            text = record.get("text", "")
            all_text.append(text)
            
            # Parse date
            created_at = record.get("createdAt", "")
            if created_at:
                post_date = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                if post_date >= thirty_days_ago:
                    posts_last_30 += 1
                if post_date >= seven_days_ago:
                    posts_last_7 += 1
            
            # Engagement metrics
            like_count = post.get("likeCount", 0)
            repost_count = post.get("repostCount", 0)
            reply_count = post.get("replyCount", 0)
            
            total_likes += like_count
            total_reposts += repost_count
            
            # Extract hashtags
            tags = re.findall(r'#(\w+)', text)
            hashtags.extend(tags)
            
            # Store recent posts
            if len(recent_posts) < 10:
                recent_posts.append({
                    "date": created_at[:10] if created_at else "Unknown",
                    "text": text[:150],
                    "likes": like_count,
                    "reposts": repost_count,
                    "replies": reply_count
                })
        
        return {
            "total_posts": len(posts),
            "posts_last_30_days": posts_last_30,
            "posts_last_7_days": posts_last_7,
            "total_likes": total_likes,
            "total_reposts": total_reposts,
            "avg_likes_per_post": round(total_likes / len(posts), 1) if posts else 0,
            "hashtags": dict(Counter(hashtags).most_common(10)),
            "recent_posts": recent_posts,
            "all_text": " ".join(all_text).lower()
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
    
    def check_coverage(self, profile, post_analysis):
        """Check keyword coverage in profile and posts"""
        goals = self.load_career_goals()
        keywords = set(goals.get("keywords", []))
        
        if not keywords:
            return {"status": "No keywords defined"}
        
        # Combine profile bio and post text
        profile_text = f"{profile.get('description', '')} {profile.get('displayName', '')}".lower()
        combined_text = f"{profile_text} {post_analysis['all_text']}"
        
        matches = [kw for kw in keywords if kw in combined_text]
        missing = [kw for kw in keywords if kw not in combined_text]
        
        return {
            "keywords_matched": matches,
            "keywords_missing": missing,
            "coverage_pct": round(len(matches) / len(keywords) * 100) if keywords else 0
        }
    
    def generate_report(self, profile, post_analysis, coverage):
        """Generate markdown report"""
        today = datetime.now().strftime("%Y-%m-%d")
        
        report = f"""# BlueSky Profile Scan: @{self.handle}
**Date:** {today}
**Profile:** https://bsky.app/profile/{self.handle}

## Profile Summary
- **Display Name:** {profile.get('displayName', 'N/A')}
- **Bio:** {profile.get('description', 'N/A')}
- **Followers:** {profile.get('followersCount', 0)}
- **Following:** {profile.get('followsCount', 0)}
- **Posts:** {profile.get('postsCount', 0)}

## Posting Activity
- **Recent Posts Analyzed:** {post_analysis['total_posts']}
- **Posts (Last 7 Days):** {post_analysis['posts_last_7_days']}
- **Posts (Last 30 Days):** {post_analysis['posts_last_30_days']}

## Engagement Metrics
- **Total Likes:** {post_analysis['total_likes']}
- **Total Reposts:** {post_analysis['total_reposts']}
- **Avg Likes/Post:** {post_analysis['avg_likes_per_post']}

## Top Hashtags
"""
        if post_analysis['hashtags']:
            for tag, count in post_analysis['hashtags'].items():
                report += f"- #{tag}: {count} times\n"
        else:
            report += "*No hashtags found*\n"
        
        report += "\n## Recent Posts\n"
        if post_analysis['recent_posts']:
            for post in post_analysis['recent_posts'][:5]:
                date = post['date'][:10] if post['date'] else 'Unknown'
                text = post['text'][:100] if post['text'] else '[No text]'
                report += f"\n**{date}** (👍 {post['likes']}, 🔁 {post['reposts']}, 💬 {post['replies']})\n"
                report += f"> {text}...\n"
        else:
            report += "*No recent posts found*\n"
        
        report += f"""
## Career Goals Alignment

### Keyword Coverage: {coverage['coverage_pct']}%

**Matched Keywords:**
"""
        if coverage['keywords_matched']:
            for kw in coverage['keywords_matched'][:20]:
                report += f"- ✓ {kw}\n"
            if len(coverage['keywords_matched']) > 20:
                report += f"*... and {len(coverage['keywords_matched']) - 20} more*\n"
        else:
            report += "*None*\n"
        
        report += "\n**Missing Keywords:**\n"
        if coverage['keywords_missing']:
            for kw in coverage['keywords_missing'][:20]:
                report += f"- ✗ {kw}\n"
            if len(coverage['keywords_missing']) > 20:
                report += f"*... and {len(coverage['keywords_missing']) - 20} more*\n"
        else:
            report += "*None - full coverage!*\n"
        
        report += f"""
## Recommendations
"""
        if coverage['coverage_pct'] < 50:
            report += "- 🔴 **Low keyword coverage** - Post more about missing topics\n"
        elif coverage['coverage_pct'] < 80:
            report += "- 🟡 **Moderate coverage** - Good foundation, consider posting about missing keywords\n"
        else:
            report += "- 🟢 **Strong coverage** - Content aligns well with career goals\n"
        
        if post_analysis['posts_last_7_days'] == 0:
            report += "- 💡 No posts in last 7 days - consider sharing insights this week\n"
        elif post_analysis['posts_last_7_days'] < 3:
            report += "- 💡 Low recent activity - consider increasing posting frequency\n"
        
        if post_analysis['avg_likes_per_post'] < 5:
            report += "- 💡 Low engagement - try posting at different times or with different topics\n"
        
        return report
    
    def run(self):
        """Execute the scan and generate report"""
        print(f"Scanning BlueSky profile: @{self.handle}")
        
        try:
            # Authenticate
            if not self.authenticate():
                print(f"✗ Authentication failed - check config/bluesky.conf")
                return None
            
            # Fetch data
            profile = self.fetch_profile()
            feed_data = self.fetch_posts()
            
            # Analyze
            post_analysis = self.analyze_posts(feed_data)
            goals = self.load_career_goals()
            coverage = self.check_coverage(profile, post_analysis)
            
            # Generate report
            report = self.generate_report(profile, post_analysis, coverage)
            
            # Save report
            today = datetime.now().strftime("%Y-%m-%d")
            output_path = self.base_dir / f"artifacts/profiles/bluesky-scan-{today}.md"
            output_path.write_text(report)
            
            print(f"✓ Report saved to {output_path}")
            return output_path
            
        except requests.exceptions.HTTPError as e:
            print(f"✗ BlueSky API error: {e}")
            if e.response.status_code == 401:
                print(f"  Check credentials in config/bluesky.conf")
            elif e.response.status_code == 404:
                print(f"  Handle not found: @{self.handle}")
            return None
        except Exception as e:
            print(f"✗ BlueSky scan failed: {e}")
            import traceback
            traceback.print_exc()
            return None

if __name__ == "__main__":
    scanner = BlueSkyScanner("jeff.at.arjentic.ai", Path(__file__).parent.parent)
    scanner.run()
