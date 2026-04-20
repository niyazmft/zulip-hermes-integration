"""
Zulip Platform Adapter for Hermes Gateway

Bi-directional integration with Zulip chat platform.
Supports stream messages (with topics) and private messages.
"""

import asyncio
import logging
import os
from typing import Optional, Dict, Any, List

try:
    import zulip
    ZULIP_AVAILABLE = True
except ImportError:
    ZULIP_AVAILABLE = False

from ..config import Platform, PlatformConfig
from ..adapter import PlatformAdapter, SessionSource

logger = logging.getLogger(__name__)


class ZulipAdapter(PlatformAdapter):
    """Zulip platform adapter for Hermes Gateway."""
    
    def __init__(self, config: PlatformConfig):
        super().__init__(config)
        
        if not ZULIP_AVAILABLE:
            raise ImportError(
                "Zulip package not installed. Run: pip install zulip"
            )
        
        # Load credentials from environment
        self.api_key = os.getenv("ZULIP_API_KEY")
        self.email = os.getenv("ZULIP_EMAIL")
        self.site = os.getenv("ZULIP_SITE")
        
        # Authorization
        self.allowed_users = self._parse_allowed_users()
        self.allow_all = os.getenv("ZULIP_ALLOW_ALL_USERS", "false").lower() == "true"
        
        # Home channel for cron deliveries
        self.home_channel = os.getenv("ZULIP_HOME_CHANNEL")
        self.home_channel_name = os.getenv("ZULIP_HOME_CHANNEL_NAME", "general")
        
        # Validate credentials
        self._validate_credentials()
        
        # Initialize client
        self.client = zulip.Client(
            email=self.email,
            api_key=self.api_key,
            site=self.site
        )
        
        # Event loop for listening
        self._listening = False
        self._event_task: Optional[asyncio.Task] = None
    
    def _validate_credentials(self):
        """Validate required credentials are present."""
        missing = []
        if not self.api_key:
            missing.append("ZULIP_API_KEY")
        if not self.email:
            missing.append("ZULIP_EMAIL")
        if not self.site:
            missing.append("ZULIP_SITE")
        
        if missing:
            raise ValueError(
                f"Zulip credentials incomplete. Missing: {', '.join(missing)}"
            )
    
    def _parse_allowed_users(self) -> List[str]:
        """Parse comma-separated allowed users from environment."""
        allowed = os.getenv("ZULIP_ALLOWED_USERS", "")
        if not allowed:
            return []
        return [u.strip() for u in allowed.split(",")]
    
    def _is_authorized(self, user_email: str) -> bool:
        """Check if user is authorized to interact with bot."""
        if self.allow_all:
            return True
        return user_email in self.allowed_users
    
    async def connect(self):
        """Initialize connection and start listening."""
        logger.info("Zulip adapter connecting...")
        
        # Test connection
        try:
            result = self.client.get_members()
            if result.get("result") != "success":
                raise ConnectionError(f"Zulip connection failed: {result}")
            logger.info("Zulip connection established")
        except Exception as e:
            logger.error(f"Zulip connection error: {e}")
            raise
        
        # Start listening
        self._listening = True
        self._event_task = asyncio.create_task(self._listen_for_events())
    
    async def disconnect(self):
        """Stop listening and close connection."""
        self._listening = False
        if self._event_task:
            self._event_task.cancel()
            try:
                await self._event_task
            except asyncio.CancelledError:
                pass
        logger.info("Zulip adapter disconnected")
    
    async def _listen_for_events(self):
        """Listen for incoming Zulip messages."""
        logger.info("Zulip adapter listening for messages...")
        
        # Use Zulip's event queue system
        queue_id = None
        
        while self._listening:
            try:
                # Register event queue if needed
                if queue_id is None:
                    register_result = self.client.register(
                        event_types=["message"],
                        fetch_event_id=0
                    )
                    queue_id = register_result["queue_id"]
                    last_event_id = register_result["last_event_id"]
                
                # Poll for events
                events = self.client.get_events(
                    queue_id=queue_id,
                    last_event_id=last_event_id
                )
                
                for event in events.get("events", []):
                    last_event_id = event["id"]
                    
                    if event.get("type") == "message":
                        await self._handle_message(event["message"])
                
            except Exception as e:
                logger.error(f"Zulip event polling error: {e}")
                queue_id = None  # Reset queue on error
                await asyncio.sleep(5)  # Backoff before retry
    
    async def _handle_message(self, message: Dict[str, Any]):
        """Process incoming Zulip message."""
        # Filter self-messages to prevent loops
        if message.get("sender_email") == self.email:
            return
        
        # Check authorization
        sender_email = message.get("sender_email", "")
        if not self._is_authorized(sender_email):
            logger.debug(f"Unauthorized user: {sender_email}")
            return
        
        # Extract message details
        message_type = message.get("type")  # "stream" or "private"
        content = message.get("content", "")
        message_id = message.get("id")
        
        # Build session source
        if message_type == "stream":
            stream_id = message.get("stream_id")
            topic = message.get("subject", "")
            
            session_source = SessionSource(
                platform=Platform.ZULIP,
                chat_id=str(stream_id),
                chat_type="stream",
                extra={
                    "stream_id": stream_id,
                    "topic": topic,
                    "message_type": "stream"
                }
            )
        else:  # private
            # For DMs, use sender's user ID
            sender_id = message.get("sender_id")
            session_source = SessionSource(
                platform=Platform.ZULIP,
                chat_id=f"dm:{sender_id}",
                chat_type="dm",
                extra={
                    "user_id": sender_id,
                    "user_email": sender_email,
                    "message_type": "private"
                }
            )
        
        # Strip Zulip mention syntax from content
        # Convert "**user**" mentions to plain text
        import re
        content = re.sub(r'\*\*([^*]+)\*\*', r'\1', content)
        
        # Dispatch to gateway
        await self.dispatch_message(session_source, content, message_id)
    
    async def send(
        self,
        chat_id: str,
        message: str,
        chat_type: str = "stream",
        extra: Optional[Dict[str, Any]] = None
    ) -> bool:
        """Send message to Zulip stream or DM."""
        extra = extra or {}
        
        try:
            if chat_type == "stream":
                # Send to stream with topic
                stream_id = int(chat_id)
                topic = extra.get("topic", self.home_channel_name)
                
                result = self.client.send_message({
                    "type": "stream",
                    "to": stream_id,
                    "topic": topic,
                    "content": message
                })
            else:  # dm
                # Send private message
                # Extract user ID from "dm:12345" format
                if chat_id.startswith("dm:"):
                    user_id = int(chat_id[3:])
                else:
                    user_id = int(chat_id)
                
                result = self.client.send_message({
                    "type": "private",
                    "to": [user_id],
                    "content": message
                })
            
            if result.get("result") == "success":
                logger.debug(f"Zulip message sent to {chat_id}")
                return True
            else:
                logger.error(f"Zulip send failed: {result}")
                return False
                
        except Exception as e:
            logger.error(f"Zulip send error: {e}")
            return False
    
    def get_home_channel(self) -> Optional[Dict[str, str]]:
        """Get home channel configuration for cron deliveries."""
        if self.home_channel:
            return {
                "platform": "zulip",
                "chat_id": self.home_channel,
                "name": self.home_channel_name
            }
        return None
