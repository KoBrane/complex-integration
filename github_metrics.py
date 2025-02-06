import asyncio
import os
import signal
import sys
from datetime import datetime, timezone
import structlog
from prometheus_client import start_http_server, Gauge, Counter, Info, REGISTRY
from prometheus_client.core import GaugeMetricFamily, CounterMetricFamily
from prometheus_amazon_managed_service import PrometheusRemoteWrite
import aiohttp
import aioboto3
from tenacity import retry, stop_after_attempt, wait_exponential

# Configure structured logging
structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer()
    ]
)
logger = structlog.get_logger()

class GitHubMetricsCollector:
    """Collects GitHub metrics and exports them to Amazon Managed Prometheus."""
    
    def __init__(self):
        self.github_token = os.getenv('GITHUB_TOKEN')
        self.org_name = os.getenv('GITHUB_ORG')
        self.amp_workspace_id = os.getenv('AMP_WORKSPACE_ID')
        self.amp_endpoint = os.getenv('AMP_ENDPOINT')
        self.region = os.getenv('AWS_REGION', 'us-west-2')
        
        if not all([self.github_token, self.org_name, self.amp_workspace_id, self.amp_endpoint]):
            logger.error("Missing required environment variables")
            sys.exit(1)
        
        # Initialize metrics
        self.active_users = Gauge(
            'github_active_users_total',
            'Number of active users in the last 24 hours',
            ['organization']
        )
        
        self.event_counter = Counter(
            'github_user_events_total',
            'Total number of events by type and user',
            ['organization', 'event_type', 'username']
        )
        
        self.api_rate_limit = Gauge(
            'github_api_rate_limit_remaining',
            'Number of API calls remaining',
            ['organization']
        )
        
        self.repo_info = Info(
            'github_repository_info',
            'Repository information',
            ['organization', 'repository']
        )
        
        # Initialize AMP remote write client
        self.remote_write = PrometheusRemoteWrite(
            workspace_id=self.amp_workspace_id,
            region=self.region
        )
        
        # Initialize async session
        self.session = None
    
    async def setup(self):
        """Set up async HTTP session with proper headers."""
        self.session = aiohttp.ClientSession(
            headers={
                'Authorization': f'token {self.github_token}',
                'Accept': 'application/vnd.github.v3+json',
                'User-Agent': 'GitHub-Metrics-Collector'
            }
        )
    
    async def cleanup(self):
        """Clean up resources."""
        if self.session:
            await self.session.close()
    
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=4, max=10)
    )
    async def fetch_github_data(self, url):
        """Fetch data from GitHub API with retry logic."""
        async with self.session.get(url) as response:
            if response.status == 200:
                return await response.json()
            else:
                logger.error("GitHub API request failed",
                           status=response.status,
                           url=url)
                response.raise_for_status()
    
    async def collect_organization_events(self):
        """Collect organization events from GitHub."""
        url = f'https://api.github.com/orgs/{self.org_name}/events'
        events = await self.fetch_github_data(url)
        
        active_users = set()
        for event in events:
            # Update event counter
            self.event_counter.labels(
                organization=self.org_name,
                event_type=event['type'],
                username=event['actor']['login']
            ).inc()
            
            # Track active users
            active_users.add(event['actor']['login'])
        
        # Update active users gauge
        self.active_users.labels(
            organization=self.org_name
        ).set(len(active_users))
    
    async def collect_rate_limit(self):
        """Collect GitHub API rate limit information."""
        url = 'https://api.github.com/rate_limit'
        rate_limit_data = await self.fetch_github_data(url)
        
        self.api_rate_limit.labels(
            organization=self.org_name
        ).set(rate_limit_data['rate']['remaining'])
    
    async def collect_repository_info(self):
        """Collect repository information."""
        url = f'https://api.github.com/orgs/{self.org_name}/repos'
        repos = await self.fetch_github_data(url)
        
        for repo in repos:
            self.repo_info.labels(
                organization=self.org_name,
                repository=repo['name']
            ).info({
                'stars': str(repo['stargazers_count']),
                'forks': str(repo['forks_count']),
                'open_issues': str(repo['open_issues_count']),
                'language': repo['language'] or 'None'
            })
    
    async def collect_metrics(self):
        """Main metrics collection loop."""
        while True:
            try:
                await asyncio.gather(
                    self.collect_organization_events(),
                    self.collect_rate_limit(),
                    self.collect_repository_info()
                )
                
                # Push metrics to AMP
                await self.remote_write.push_metrics(REGISTRY)
                
                logger.info("Metrics collection completed successfully")
                
            except Exception as e:
                logger.error("Error collecting metrics",
                           error=str(e),
                           exc_info=True)
            
            await asyncio.sleep(60)  # Collect every minute

async def main():
    """Main entry point for the metrics collector."""
    # Start Prometheus HTTP server for local scraping
    start_http_server(8000)
    logger.info("Prometheus metrics server started", port=8000)
    
    collector = GitHubMetricsCollector()
    await collector.setup()
    
    # Handle graceful shutdown
    loop = asyncio.get_event_loop()
    signals = (signal.SIGHUP, signal.SIGTERM, signal.SIGINT)
    for s in signals:
        loop.add_signal_handler(
            s, lambda s=s: asyncio.create_task(shutdown(s, collector, loop))
        )
    
    try:
        await collector.collect_metrics()
    finally:
        await collector.cleanup()

async def shutdown(signal, collector, loop):
    """Cleanup tasks tied to the service's shutdown."""
    logger.info("Received exit signal", signal=signal.name)
    
    tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    [task.cancel() for task in tasks]
    
    logger.info("Cancelling outstanding tasks")
    await asyncio.gather(*tasks, return_exceptions=True)
    await collector.cleanup()
    loop.stop()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Service stopped by user")
    except Exception as e:
        logger.error("Unhandled exception", error=str(e), exc_info=True)
        sys.exit(1)