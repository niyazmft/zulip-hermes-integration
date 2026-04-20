# Example: Cron Job Deliveries to Zulip
#
# This example shows how to configure cron jobs to deliver results to Zulip streams.

from hermes_tools import cronjob

# Example 1: Daily project pulse to Zulip stream
def setup_daily_pulse():
    """Create a daily cron job that delivers to Zulip."""
    cronjob(
        action="create",
        prompt="""
        Check Linear for new issues created in the last 24 hours.
        Summarize them by team and priority.
        Include links to each issue.
        """,
        schedule="0 9 * * *",  # Every day at 9 AM
        deliver="zulip:573423",  # Pulse stream ID
        name="Daily Linear Pulse"
    )
    print("Daily pulse cron job created")

# Example 2: Weekly security scan results
def setup_security_scan():
    """Weekly security scan delivered to Zulip."""
    cronjob(
        action="create",
        prompt="""
        Run security checks on the repository:
        1. Check for exposed secrets in .env files
        2. Scan for outdated dependencies
        3. Review recent commits for security issues
        Provide a summary with severity levels.
        """,
        schedule="0 10 * * 1",  # Every Monday at 10 AM
        deliver="zulip:573423",
        extra={"topic": "security"},  # Specific topic
        name="Weekly Security Scan"
    )
    print("Security scan cron job created")

# Example 3: GitHub PR notifications
def setup_pr_notifications():
    """Notify Zulip channel when PRs need review."""
    cronjob(
        action="create",
        prompt="""
        Check GitHub for pull requests that:
        - Are open and not drafted
        - Have no reviews or changes requested
        - Were created in the last 48 hours
        List them with links and assignees.
        """,
        schedule="0 14 * * *",  # Every day at 2 PM
        deliver="zulip:573423",
        extra={"topic": "code-review"},
        name="PR Review Reminder"
    )
    print("PR notification cron job created")

# Example 4: One-time report
def run_one_time_report():
    """Run a one-time analysis and deliver to Zulip."""
    cronjob(
        action="create",
        prompt="""
        Analyze the project's technical debt:
        - Count TODO/FIXME comments
        - Identify files with most complexity
        - List outdated dependencies
        Provide actionable recommendations.
        """,
        schedule="now",  # Run immediately
        deliver="zulip:573423",
        extra={"topic": "tech-debt"},
        name="Tech Debt Analysis",
        repeat=1  # Run only once
    )
    print("One-time report scheduled")

if __name__ == "__main__":
    # Run the setup you need
    setup_daily_pulse()
    # setup_security_scan()
    # setup_pr_notifications()
    # run_one_time_report()
