import os
import re
import uuid
import asyncio
import logging
from logging.handlers import RotatingFileHandler
from typing import Optional, Dict, Any, Literal, Union, List
from datetime import datetime, timezone, timedelta
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
from collections import defaultdict
from aiohttp import web

try:
    from supabase import create_client, Client
    SUPABASE_AVAILABLE = True
except ImportError:
    SUPABASE_AVAILABLE = False
    Client = None

load_dotenv()

# Constants
WORDLE_BOT_ID = 1211781489931452447
MAX_WORDLE_GUESSES = 6

# Configure logging
log_dir = 'logs'
os.makedirs(log_dir, exist_ok=True)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Console handler
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
console_formatter = logging.Formatter(
    '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
console_handler.setFormatter(console_formatter)

# File handler with rotation
file_handler = RotatingFileHandler(
    os.path.join(log_dir, 'wordlestatsbot.log'),
    maxBytes=10 * 1024 * 1024,  # 10MB
    backupCount=5
)
file_handler.setLevel(logging.INFO)
file_formatter = logging.Formatter(
    '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
file_handler.setFormatter(file_formatter)

logger.addHandler(console_handler)
logger.addHandler(file_handler)

# Initialize Supabase client
supabase: Optional[Client] = None
if SUPABASE_AVAILABLE:
    supabase_url = os.getenv('SUPABASE_URL')
    supabase_key = os.getenv('SUPABASE_KEY')
    if supabase_url and supabase_key:
        supabase = create_client(supabase_url, supabase_key)
        logger.info('Supabase client initialized')
    else:
        logger.warning('Supabase URL or key not found in environment')
else:
    logger.warning('Supabase library not available')

# Set up intents
intents = discord.Intents.default()
intents.message_content = True  # Required to read message content
intents.members = True  # Required for accurate leaderboards (needs Dev Portal setting)

bot = commands.Bot(command_prefix='!', intents=intents)

# Race condition prevention: locks per guild
processing_locks: Dict[int, asyncio.Lock] = {}


class WordleGameResult:
    """Represents a single user's result from a Wordle game."""
    def __init__(self, user_id: int, username: str, won: bool, guesses: int):
        self.user_id = user_id
        self.username = username
        self.won = won
        self.guesses = guesses

    def __repr__(self):
        return f"WordleGameResult(user_id={self.user_id}, won={self.won}, guesses={self.guesses})"


async def execute_supabase(func, *args, **kwargs) -> Any:
    """
    Execute a Supabase call in a separate thread to avoid blocking the event loop.
    
    Args:
        func: The callable to execute (e.g. query.execute)
        *args: Arguments to pass to the callable
        **kwargs: Keyword arguments to pass to the callable
        
    Returns:
        The result of the callable
    """
    if not supabase:
        logger.warning('Supabase not configured. Skipping database call.')
        return None
        
    return await asyncio.to_thread(func, *args, **kwargs)


def get_guild_lock(guild_id: int) -> asyncio.Lock:
    """Get or create a lock for a specific guild."""
    if guild_id not in processing_locks:
        processing_locks[guild_id] = asyncio.Lock()
    return processing_locks[guild_id]


def is_wordle_bot_message(message: discord.Message) -> bool:
    """
    Check if a message is from the Wordle bot.
    
    Args:
        message: Discord message object
        
    Returns:
        bool: True if message is from Wordle bot
    """
    if not message or not message.author:
        return False
    
    # Check by bot ID first (most reliable)
    if message.author.id == WORDLE_BOT_ID:
        return True
    
    # Fallback to name check for backwards compatibility
    if message.author.name == 'Wordle':
        return True
    
    return False


def generate_uuid_from_user_id(user_id: int) -> str:
    """
    Generate a deterministic UUID from user_id.
    
    Args:
        user_id: Discord user ID
        
    Returns:
        str: UUID string
    """
    namespace = uuid.UUID('6ba7b810-9dad-11d1-80b4-00c04fd430c8')
    unique_string = str(user_id)
    return str(uuid.uuid5(namespace, unique_string))


def initialize_user_stats(user_id: int, username: str) -> Dict[str, Any]:
    """
    Initialize a new user stats dictionary.
    
    Args:
        user_id: Discord user ID
        username: Discord username
        
    Returns:
        dict: Initialized user stats dictionary
    """
    return {
        'user_id': user_id,
        'username': username,
        'total_games': 0,
        'total_guesses': 0,
        'wins': 0,
        'losses': 0,
    }



async def update_user_stats_atomic(result: WordleGameResult, message_date: datetime) -> None:
    """
    Update user statistics atomically using a Supabase RPC call.
    
    Args:
        result: WordleGameResult object containing game data
        message_date: The date of the message (timezone-aware)
    """
    if not supabase:
        return

    try:
        # Call the stored procedure 'increment_player_stats'
        params = {
            'p_user_id': str(result.user_id),
            'p_username': result.username,
            'p_won': result.won,
            'p_guesses': result.guesses,
            'p_message_date': message_date.isoformat()
        }
        
        await execute_supabase(
            supabase.rpc('increment_player_stats', params).execute
        )
        logger.debug(f"Atomically updated stats for user {result.user_id}")
        
    except Exception as e:
        logger.error(
            f'Error updating stats atomically for user {result.user_id}: {e}',
            exc_info=True
        )


def convert_supabase_stats_to_processing_format(
    supabase_stats: Optional[Dict[int, Dict[str, Any]]]
) -> Dict[int, Dict[str, Any]]:
    """
    Convert Supabase stats format to the format expected by
    process_wordle_message.
    
    Args:
        supabase_stats: Dictionary from get_user_stats_from_supabase
            (with calculated fields)
        
    Returns:
        dict: Dictionary in format expected by process_wordle_message
            (raw counts only)
    """
    if not supabase_stats:
        return {}
    
    processing_stats = {}
    for user_id, stats in supabase_stats.items():
        processing_stats[user_id] = {
            'user_id': user_id,
            'username': stats['username'],
            'total_games': stats['total_games'],
            'total_guesses': stats['total_guesses'],
            'wins': stats['wins'],
            'losses': stats['losses'],
        }
    
    return processing_stats


async def extract_users_from_content(
    message: discord.Message,
    content: str,
    guild_override: Optional[discord.Guild] = None
) -> List[discord.Member]:
    """
    Extract users from message content, handling both proper mentions
    (<@user_id>) and plain text mentions (@displayname).
    
    Args:
        message: Discord message object
        content: Message content string to extract users from
        guild_override: Optional guild object to use instead of message.guild
            (useful when guild cache is pre-populated)
        
    Returns:
        List of discord.Member objects that were found
    """
    user_objects = []
    
    # Use guild_override if provided, else fallback to message.guild
    guild = guild_override or message.guild
    
    # Extract proper Discord mentions (<@user_id> or <@!user_id>)
    mention_ids = re.findall(r'<@!?(\d+)>', content)
    for user_id_str in mention_ids:
        user_id = int(user_id_str)
        # Try to get user from message.mentions
        user = discord.utils.get(message.mentions, id=user_id)
        if user:
            user_objects.append(user)
        else:
            # User not in message.mentions - try to fetch them
            # Try local cache first (fast, no API call)
            if guild:
                user = guild.get_member(user_id)
                if user:
                    user_objects.append(user)
                else:
                    # Only fetch from API if not in cache (slow fallback)
                    try:
                        user = await guild.fetch_member(user_id)
                        user_objects.append(user)
                    except discord.NotFound:
                        logger.warning(
                            f'User {user_id} not found in guild. '
                            f'Message: {message.id}'
                        )
                    except discord.HTTPException as e:
                        logger.warning(
                            f'HTTP error fetching user {user_id}: {e}. '
                            f'Message: {message.id}'
                        )
                    except Exception as e:
                        logger.error(
                            f'Error fetching user {user_id}: {e}, '
                            f'Message: {message.id}',
                            exc_info=True
                        )
            else:
                logger.warning(
                    f'No guild context for user {user_id}. '
                    f'Message: {message.id}'
                )
    
    # Handle plain text mentions (e.g., "@rice" or "@THE President" instead of "<@123456789>")
    # First, remove proper mentions from the string to avoid false matches
    content_clean = re.sub(r'<@!?\d+>', '', content)
    
    if guild:
        # Find all @ positions in the cleaned content
        at_positions = [m.start() for m in re.finditer(r'@', content_clean)]
        
        for i, at_pos in enumerate(at_positions):
            # Get the text after the @ symbol, but stop at the next @ if it exists
            if i + 1 < len(at_positions):
                # Stop at the next @ symbol
                next_at_pos = at_positions[i + 1]
                text_after_at = content_clean[at_pos + 1:next_at_pos].strip()
            else:
                # This is the last @, get all remaining text
                text_after_at = content_clean[at_pos + 1:].strip()
            
            if not text_after_at:
                continue
            
            # Try progressively longer substrings (1 word, 2 words, 3 words, etc.)
            # up to 5 words (reasonable limit for display names)
            words = text_after_at.split()
            max_words = min(len(words), 5)  # Limit to 5 words max
            
            user = None
            matched_username = None
            
            for word_count in range(1, max_words + 1):
                # Try matching with this many words
                candidate = ' '.join(words[:word_count])
                
                # Skip if this is just a number (likely a user ID from a malformed mention)
                if candidate.isdigit():
                    continue
                
                # Skip if this username was already found via proper mention
                if any(u.name == candidate or u.display_name == candidate 
                       for u in user_objects):
                    continue
                
                # Try to find user in guild by display name first (most common case)
                user = discord.utils.get(
                    guild.members,
                    display_name=candidate
                )
                # Fallback to username if display name doesn't match
                if not user:
                    user = discord.utils.get(
                        guild.members,
                        name=candidate
                    )
                
                # Try case-insensitive search if exact match failed
                if not user:
                    user = discord.utils.find(
                        lambda m: m.display_name.lower() == candidate.lower() or
                                 m.name.lower() == candidate.lower(),
                        guild.members
                    )
                
                if user:
                    matched_username = candidate
                    break  # Found a match, stop trying longer strings
            
            if user:
                user_objects.append(user)
            else:
                # Try single word as fallback (original behavior)
                first_word = words[0] if words else None
                if first_word and not first_word.isdigit():
                    # Already tried above, skip
                    pass
    
    return user_objects


async def parse_nobody_message_content(
    message: discord.Message,
    guild_override: Optional[discord.Guild] = None
) -> List[WordleGameResult]:
    """
    Parse a "Nobody got yesterday's Wordle" message.
    
    Args:
        message: Discord message object
        guild_override: Optional guild object to use instead of message.guild
            (useful when guild cache is pre-populated)
        
    Returns:
        List of WordleGameResult objects
    """
    results = []
    if not message or not message.content:
        return results
    
    content = message.content
    
    # Check if this matches the pattern
    if not re.search(r"Nobody got yesterday's Wordle", content, re.IGNORECASE):
        return results
    
    # Extract all mentioned users from the message
    user_objects = await extract_users_from_content(message, content, guild_override)
    
    for user in user_objects:
        results.append(WordleGameResult(
            user_id=user.id,
            username=user.name,
            won=False,
            guesses=MAX_WORDLE_GUESSES
        ))
        
    return results


async def parse_wordle_message_content(
    message: discord.Message,
    guild_override: Optional[discord.Guild] = None
) -> List[WordleGameResult]:
    """
    Parse a standard Wordle message.
    
    Args:
        message: Discord message object
        guild_override: Optional guild object to use instead of message.guild
            (useful when guild cache is pre-populated)
        
    Returns:
        List of WordleGameResult objects
    """
    results = []
    if not message or not message.content:
        return results
    
    content = message.content
    
    # Extract day streak number (validation only)
    streak_match = re.search(
        r'Your group is on (a|an) (\d+) day streak', content
    )
    if not streak_match:
        return results
    
    # Use guild_override if provided, else fallback to message.guild
    guild = guild_override or message.guild
    
    # Parse results section
    lines = content.split('\n')
    
    for line in lines:
        line = line.strip()
        if not line:
            continue
        
        # Check if this line contains results
        match = re.match(r'(:crown:\s+|👑\s+)?([1-6X])/6:\s+(.+)', line)
        if match:
            guess_str = match.group(2)
            users_str = match.group(3)
            
            # Determine guess count and win/loss status
            if guess_str == 'X':
                guess_count = MAX_WORDLE_GUESSES
                won = False
            else:
                guess_count = int(guess_str)
                won = True
            
            # Extract user mentions from the line
            # Handle Discord mention format (<@user_id> or <@!user_id>)
            user_objects = []
            unresolved_user_ids = []  # Track user IDs we couldn't resolve but want to track
            mention_ids = re.findall(r'<@!?(\d+)>', users_str)
            for user_id_str in mention_ids:
                user_id = int(user_id_str)
                # Try to get user from message.mentions
                user = discord.utils.get(
                    message.mentions, id=user_id
                )
                if user:
                    user_objects.append(user)
                else:
                    # User not in message.mentions - try to fetch them
                    if guild:
                        # Try local cache first (fast, no API call)
                        user = guild.get_member(user_id)
                        if user:
                            user_objects.append(user)
                        else:
                            # Only fetch from API if not in cache (slow fallback)
                            try:
                                user = await guild.fetch_member(user_id)
                                user_objects.append(user)
                            except discord.NotFound:
                                # User not found in guild - still track them with user_id
                                unresolved_user_ids.append((user_id, f'User_{user_id}'))
                                logger.warning(
                                    f'User {user_id} not found in guild. '
                                    f'Will track with placeholder name. '
                                    f'Message: {message.id}'
                                )
                            except discord.HTTPException as e:
                                unresolved_user_ids.append((user_id, f'User_{user_id}'))
                                logger.warning(
                                    f'HTTP error fetching user {user_id}: {e}. '
                                    f'Will track with placeholder name. '
                                    f'Message: {message.id}'
                                )
                            except Exception as e:
                                unresolved_user_ids.append((user_id, f'User_{user_id}'))
                                logger.error(
                                    f'Error fetching user {user_id}: {e}, '
                                    f'Message: {message.id}',
                                    exc_info=True
                                )
                    else:
                        # No guild context - track with placeholder
                        unresolved_user_ids.append((user_id, f'User_{user_id}'))
                        logger.warning(
                            f'No guild context for user {user_id}. '
                            f'Will track with placeholder name. '
                            f'Message: {message.id}'
                        )
            
            # Handle plain text mentions
            users_str_clean = re.sub(r'<@!?\d+>', '', users_str)
            
            if guild:
                # Find all @ positions in the cleaned string
                at_positions = [m.start() for m in re.finditer(r'@', users_str_clean)]
                
                for i, at_pos in enumerate(at_positions):
                    if i + 1 < len(at_positions):
                        next_at_pos = at_positions[i + 1]
                        text_after_at = users_str_clean[at_pos + 1:next_at_pos].strip()
                    else:
                        text_after_at = users_str_clean[at_pos + 1:].strip()
                    
                    if not text_after_at:
                        continue
                    
                    words = text_after_at.split()
                    max_words = min(len(words), 5)
                    
                    user = None
                    matched_username = None
                    
                    for word_count in range(1, max_words + 1):
                        candidate = ' '.join(words[:word_count])
                        
                        if candidate.isdigit():
                            continue
                        
                        if any(u.name == candidate or u.display_name == candidate 
                               for u in user_objects):
                            continue
                        
                        user = discord.utils.get(
                            guild.members,
                            display_name=candidate
                        )
                        if not user:
                            user = discord.utils.get(
                                guild.members,
                                name=candidate
                            )
                        
                        if not user:
                            user = discord.utils.find(
                                lambda m: m.display_name.lower() == candidate.lower() or
                                         m.name.lower() == candidate.lower(),
                                guild.members
                            )
                        
                        if user:
                            matched_username = candidate
                            break
                    
                    if user:
                        user_objects.append(user)
                        logger.info(
                            f'Resolved plain text mention "@{matched_username}" to user '
                            f'{user.id} ({user.display_name}) in message {message.id}'
                        )
                    else:
                        first_word = words[0] if words else None
                        if first_word and not first_word.isdigit():
                            logger.warning(
                                f'Could not resolve plain text mention starting with "@{first_word}" '
                                f'to a user. Cannot track without user_id. Message: {message.id}'
                            )
            else:
                 logger.warning(
                    f'Could not resolve plain text mentions (no guild context). '
                    f'Cannot track without user_id. Message: {message.id}'
                )
            
            # Add results for resolved users
            for user in user_objects:
                results.append(WordleGameResult(
                    user_id=user.id,
                    username=user.name,
                    won=won,
                    guesses=guess_count
                ))
            
            # Add results for unresolved users
            for user_id, placeholder_username in unresolved_user_ids:
                results.append(WordleGameResult(
                    user_id=user_id,
                    username=placeholder_username,
                    won=won,
                    guesses=guess_count
                ))
                
    return results


async def process_nobody_got_wordle_message(
    message: discord.Message,
    user_stats: Dict[int, Dict[str, Any]],
    guild_override: Optional[discord.Guild] = None
) -> None:
    """
    Process a "Nobody got yesterday's Wordle" message.
    Everyone mentioned gets +1 game, +1 loss, and +6 total guesses.
    
    Args:
        message: Discord message object containing the message
        user_stats: Dictionary keyed by user_id to update with user statistics
        guild_override: Optional guild object to use instead of message.guild
            (useful when guild cache is pre-populated)
    """
    results = await parse_nobody_message_content(message, guild_override)
    
    for res in results:
        if res.user_id not in user_stats:
            user_stats[res.user_id] = initialize_user_stats(res.user_id, res.username)
            
        user_stats[res.user_id]['total_games'] += 1
        user_stats[res.user_id]['losses'] += 1
        user_stats[res.user_id]['total_guesses'] += res.guesses


async def process_wordle_message(
    message: discord.Message,
    user_stats: Dict[int, Dict[str, Any]],
    guild_override: Optional[discord.Guild] = None
) -> None:
    """
    Process a single Wordle message and update user statistics.
    
    Args:
        message: Discord message object containing Wordle results
        user_stats: Dictionary keyed by user_id to update with user
            statistics
        guild_override: Optional guild object to use instead of message.guild
            (useful when guild cache is pre-populated)
    """
    results = await parse_wordle_message_content(message, guild_override)
    
    for res in results:
        if res.user_id not in user_stats:
            user_stats[res.user_id] = initialize_user_stats(res.user_id, res.username)
            
        user_stats[res.user_id]['total_games'] += 1
        if res.won:
            user_stats[res.user_id]['wins'] += 1
        else:
            user_stats[res.user_id]['losses'] += 1
        user_stats[res.user_id]['total_guesses'] += res.guesses


def calculate_statistics(
    user_stats: Dict[int, Dict[str, Any]]
) -> Dict[int, Dict[str, Any]]:
    """
    Calculate final statistics from user_stats dictionary.
    
    Args:
        user_stats: Dictionary keyed by user_id with user statistics
        
    Returns:
        dict: Dictionary mapping user_ids to their calculated statistics
    """
    stats_summary = {}
    for user_id, stats in user_stats.items():
        total_games = stats['total_games']
        total_guesses = stats['total_guesses']
        wins = stats['wins']
        losses = stats['losses']
        
        # Calculate derived statistics
        win_rate = (wins / total_games * 100) if total_games > 0 else 0
        loss_rate = (losses / total_games * 100) if total_games > 0 else 0
        # Average guess includes all games (wins + losses where
        # losses count as 6)
        avg_guess = (
            (total_guesses / total_games) if total_games > 0 else 0
        )
        
        stats_summary[user_id] = {
            'user_id': user_id,
            'username': stats.get('username', 'Unknown'),
            'total_games': total_games,
            'total_guesses': total_guesses,
            'wins': wins,
            'losses': losses,
            'win_rate': win_rate,
            'loss_rate': loss_rate,
            'avg_guess': avg_guess
        }
    
    return stats_summary


async def store_user_stats_in_supabase(
    user_stats: Dict[int, Dict[str, Any]]
) -> None:
    """
    Store user statistics in Supabase.
    
    Handles multiple /setup runs by comparing local stats with existing database
    stats. Only updates users when local total_games is greater than database
    total_games. New users are always added. Users with equal or fewer local
    games are skipped to prevent overwriting more complete data.
    
    Args:
        user_stats: Dictionary keyed by user_id with user statistics
    """
    if not supabase:
        logger.warning(
            'Supabase not configured. Skipping database storage.'
        )
        return
    
    if not user_stats:
        return
    
    stats_summary = calculate_statistics(user_stats)
    
    # Fetch existing stats from database for all users in local stats
    user_ids = list(stats_summary.keys())
    existing_stats = await get_user_stats_from_supabase(user_ids)
    
    records = []
    added_count = 0
    updated_count = 0
    skipped_count = 0
    
    # Get current timestamp for last_updated_date (once for all records in this batch)
    # CRITICAL: Set to yesterday so that if a new Wordle message comes in today (UTC),
    # it won't be blocked by the date check.
    current_timestamp = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    
    for user_id, stats in stats_summary.items():
        record_uuid = generate_uuid_from_user_id(user_id)
        local_total_games = stats['total_games']
        
        # Check if user exists in database
        if user_id not in existing_stats:
            # User doesn't exist in DB - add them
            record = {
                'id': record_uuid,
                'user_id': str(user_id),
                'username': stats['username'],
                'total_games': stats['total_games'],
                'total_guesses': stats['total_guesses'],
                'wins': stats['wins'],
                'losses': stats['losses'],
                'win_rate': round(stats['win_rate'], 2),
                'loss_rate': round(stats['loss_rate'], 2),
                'avg_guess': round(stats['avg_guess'], 2),
                'last_updated_date': current_timestamp
            }
            records.append(record)
            added_count += 1
        else:
            # User exists in DB - compare total_games
            db_total_games = existing_stats[user_id]['total_games']
            
            if local_total_games > db_total_games:
                # Local has more games - update with new stats
                record = {
                    'id': record_uuid,
                    'user_id': str(user_id),
                    'username': stats['username'],
                    'total_games': stats['total_games'],
                    'total_guesses': stats['total_guesses'],
                    'wins': stats['wins'],
                    'losses': stats['losses'],
                    'win_rate': round(stats['win_rate'], 2),
                    'loss_rate': round(stats['loss_rate'], 2),
                    'avg_guess': round(stats['avg_guess'], 2),
                    'last_updated_date': current_timestamp
                }
                records.append(record)
                updated_count += 1
            else:
                # Local has equal or fewer games - skip to avoid overwriting
                skipped_count += 1
                logger.debug(
                    f'Skipped updating user {user_id}: local games ({local_total_games}) '
                    f'<= database games ({db_total_games})'
                )
    
    # Upsert only the filtered records
    if records:
        try:
            await execute_supabase(
                supabase.table('user_stats').upsert(records).execute
            )
            logger.info(
                f'Stored {len(records)} user statistics records in Supabase: '
                f'{added_count} added, {updated_count} updated, {skipped_count} skipped'
            )
        except Exception as e:
            logger.error(
                f'Error storing statistics in Supabase: {e}',
                exc_info=True
            )
    elif skipped_count > 0:
        # All users were skipped
        logger.info(
            f'No records to store: {skipped_count} users skipped '
            f'(local stats had equal or fewer games than database)'
        )


async def get_user_stats_from_supabase(
    user_id: Optional[Union[int, List[int]]] = None
) -> Dict[int, Dict[str, Any]]:
    """
    Retrieve user statistics from Supabase.
    
    Args:
        user_id: Optional Discord user ID (int) or list of user IDs (List[int])
            to filter by. If None, returns all users.
        
    Returns:
        dict: Dictionary keyed by user_id with user statistics.
            Returns empty dict if error/not found
    """
    if not supabase:
        logger.warning(
            'Supabase not configured. Cannot retrieve statistics.'
        )
        return {}
    
    try:
        # If user_id is provided, filter by it
        if user_id is not None:
            if isinstance(user_id, list):
                # Batch queries for large lists to prevent URL length limits
                # Supabase .in_() can handle large lists, but batching is safer
                # and prevents potential API errors
                BATCH_SIZE = 50
                user_stats = {}
                
                # Process in batches
                for i in range(0, len(user_id), BATCH_SIZE):
                    batch = user_id[i:i + BATCH_SIZE]
                    user_id_strs = [str(uid) for uid in batch]
                    
                    query = supabase.table('user_stats').select('*')
                    query = query.in_('user_id', user_id_strs)
                    response = await execute_supabase(query.execute)
                    
                    if response and response.data:
                        for record in response.data:
                            uid = int(record['user_id'])
                            user_stats[uid] = {
                                'user_id': uid,
                                'username': record['username'],
                                'total_games': record['total_games'],
                                'total_guesses': record['total_guesses'],
                                'wins': record['wins'],
                                'losses': record['losses'],
                                'win_rate': record['win_rate'],
                                'loss_rate': record['loss_rate'],
                                'avg_guess': record['avg_guess']
                            }
                
                return user_stats
            else:
                # Filter by single user ID
                query = supabase.table('user_stats').select('*')
                query = query.eq('user_id', str(user_id))
                response = await execute_supabase(query.execute)
        else:
            # No filter - get all users
            query = supabase.table('user_stats').select('*')
            response = await execute_supabase(query.execute)
        
        if not response or not response.data:
            return {}
        
        # Convert to dictionary keyed by user_id
        user_stats = {}
        for record in response.data:
            uid = int(record['user_id'])
            user_stats[uid] = {
                'user_id': uid,
                'username': record['username'],
                'total_games': record['total_games'],
                'total_guesses': record['total_guesses'],
                'wins': record['wins'],
                'losses': record['losses'],
                'win_rate': record['win_rate'],
                'loss_rate': record['loss_rate'],
                'avg_guess': record['avg_guess']
            }
        
        return user_stats
        
    except Exception as e:
        logger.error(
            f'Error retrieving statistics from Supabase: {e}',
            exc_info=True
        )
        return {}


@bot.event
async def on_ready():
    """Event handler for when the bot is ready."""
    logger.info(f'Logged in as {bot.user}')
    
    # Log registered commands for debugging
    registered_commands = [cmd.name for cmd in bot.tree.get_commands()]
    logger.info(f'Registered commands in tree: {registered_commands}')
    
    try:
        # For faster testing, sync to a specific guild first
        # Set TEST_GUILD_ID in .env for instant command updates during development
        test_guild_id = os.getenv('TEST_GUILD_ID')
        
        if test_guild_id:
            guild = discord.Object(id=int(test_guild_id))
            synced = await bot.tree.sync(guild=guild)
            logger.info(f'Synced {len(synced)} command(s) to guild {test_guild_id}: {[cmd.name for cmd in synced]}')
        else:
            synced = await bot.tree.sync()
            logger.info(f'Synced {len(synced)} command(s) globally')
    except Exception as e:
        logger.error(f'Failed to sync commands: {e}', exc_info=True)


@bot.event
async def on_guild_join(guild: discord.Guild):
    """
    Event handler for when the bot joins a guild.
    
    Args:
        guild: Discord guild object
    """
    logger.info(f'Joined {guild.name} ({guild.id})')
    if guild.text_channels:
        channel = guild.text_channels[0]
        try:
            await channel.send(
                f'Hello! I have joined {guild.name} ({guild.id})'
            )
        except Exception as e:
            logger.error(
                f'Error sending join message: {e}',
                exc_info=True
            )


@bot.event
async def on_message(message: discord.Message):
    """
    Event handler for when a message is received.
    
    Args:
        message: Discord message object
    """
    # Ignore messages from the bot itself
    if message.author == bot.user:
        await bot.process_commands(message)
        return
    
    # Check if this is a Wordle bot message
    if is_wordle_bot_message(message) and message.content:
        # Check for streak message pattern
        is_streak_message = re.search(r'Your group is on (a|an) \d+ day streak', message.content)
        # Check for "Nobody got yesterday's Wordle" message pattern
        is_nobody_got_message = re.search(r"Nobody got yesterday's Wordle", message.content, re.IGNORECASE)
        
        if is_streak_message or is_nobody_got_message:
            if message.guild:
                guild_id = message.guild.id
                lock = get_guild_lock(guild_id)
                
                async with lock:
                    try:
                        # Process the new Wordle message
                        results = []
                        if is_streak_message:
                            results = await parse_wordle_message_content(message)
                        elif is_nobody_got_message:
                            results = await parse_nobody_message_content(message)
                        
                        if not results:
                            return
                        
                        # Get message creation date (already UTC in discord.py, but ensure it's timezone-aware)
                        message_date = message.created_at
                        if message_date.tzinfo is None:
                            message_date = message_date.replace(tzinfo=timezone.utc)
                        
                        # Store updated statistics using atomic updates
                        # The database function handles deduplication based on message_date
                        for result in results:
                            await update_user_stats_atomic(result, message_date)
                            
                        logger.info(
                            f'Processed new Wordle message from channel '
                            f'{message.channel.id} in guild {guild_id}. '
                            f'Sent update requests for {len(results)} users.'
                        )
                    except Exception as e:
                        logger.error(
                            f'Error processing Wordle message: {e}',
                            exc_info=True
                        )
    
    # Process commands (important: don't forget this!)
    await bot.process_commands(message)


@bot.event
async def on_command_error(
    ctx: commands.Context,
    error: Exception
):
    """
    Global error handler for prefix commands.
    
    Args:
        ctx: Command context
        error: Exception that occurred
    """
    if isinstance(error, commands.CommandNotFound):
        return
    
    if isinstance(error, commands.MissingPermissions):
        await ctx.send(
            'You do not have permission to use this command. Please contact a server administrator if you believe this is an error.'
        )
        return
    
    if isinstance(error, commands.CommandOnCooldown):
        await ctx.send(
            f'This command is on cooldown. Try again in {error.retry_after:.2f} seconds.'
        )
        return
    
    logger.error(
        f'Error in command {ctx.command}: {error}',
        exc_info=True
    )
    await ctx.send(
        'An error occurred while processing your command. Please try again later.'
    )


@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError
):
    """
    Global error handler for slash commands.
    
    Args:
        interaction: Discord interaction object
        error: Exception that occurred
    """
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message(
            'You do not have permission to use this command. Please contact a server administrator if you believe this is an error.',
            ephemeral=True
        )
        return
    
    logger.error(
        f'Error in slash command {interaction.command.name if interaction.command else "unknown"}: {error}',
        exc_info=True
    )
    
    # Try to respond if not already responded
    try:
        if interaction.response.is_done():
            await interaction.followup.send(
                'An error occurred while processing your command. Please try again later.',
                ephemeral=True
            )
        else:
            await interaction.response.send_message(
                'An error occurred while processing your command. Please try again later.',
                ephemeral=True
            )
    except:
        pass


@bot.tree.command(name='setup', description='Set up the Wordle Stats Bot')
@app_commands.checks.has_permissions(manage_guild=True)
async def setup(interaction: discord.Interaction):
    """
    Set up the Wordle Stats Bot by processing historical messages.
    
    Requires manage_guild permission.
    """
    channel = interaction.channel
    guild = interaction.guild
    
    if not guild:
        await interaction.response.send_message(
            'This command can only be used in a server. Please use it in a server channel.'
        )
        return
    
    if not isinstance(channel, discord.TextChannel):
        await interaction.response.send_message(
            'This command can only be used in a text channel.'
        )
        return
    
    # Send initial message to acknowledge the interaction
    await interaction.response.send_message('Setting up Wordle Stats Bot...')
    
    try:
        # Check if Wordle bot has sent messages in this channel
        # First check last 10 messages (greedy approach)
        wordle_bot_found = False
        async for message in channel.history(limit=10):
            if is_wordle_bot_message(message):
                wordle_bot_found = True
                break
        
        # If not found in last 10, check last 100 messages
        if not wordle_bot_found:
            async for message in channel.history(limit=100):
                if is_wordle_bot_message(message):
                    wordle_bot_found = True
                    break
        
        if wordle_bot_found:            
            # Cache all guild members to avoid API rate limits during processing
            # This is crucial for performance when resolving mentions
            if guild:
                await guild.chunk()
            
            # Initialize statistics dictionary and counters
            user_stats = {}
            total_scraped = 0
            wordle_bot_messages_found = 0
            games_found = 0
            
            # Stream messages and process them one-by-one to avoid memory issues
            # Process all messages in channel history (no limit)
            async for message in channel.history(limit=None):
                total_scraped += 1
                
                # First check if it's from Wordle bot
                if is_wordle_bot_message(message):
                    wordle_bot_messages_found += 1
                    
                    if message.content:
                        # Check if it's a results message with streak
                        if re.search(r'Your group is on (a|an) \d+ day streak', message.content):
                            # Process message immediately instead of storing in list
                            # Pass the cached guild object to use pre-populated member cache
                            await process_wordle_message(message, user_stats, guild_override=guild)
                            games_found += 1
                        # Check if it's a "Nobody got yesterday's Wordle" message
                        elif re.search(r"Nobody got yesterday's Wordle", message.content, re.IGNORECASE):
                            # Process message immediately
                            # Pass the cached guild object to use pre-populated member cache
                            await process_nobody_got_wordle_message(message, user_stats, guild_override=guild)
                            games_found += 1
                        else:
                            # Wordle bot message doesn't match expected patterns - skip it
                            pass
            
            if games_found > 0:
                # Store statistics in Supabase
                await store_user_stats_in_supabase(user_stats)
                
                await interaction.followup.send(
                    f'{games_found} games found. '
                    f'Wordle Stats Bot setup complete.'
                )
            else:
                await interaction.followup.send(
                    'No Wordle results found in the channel. '
                    'Wordle Stats Bot setup complete.'
                )
        else:
            await interaction.followup.send(
                'Error: Wordle Bot messages not found in this channel. Please make sure the Wordle Bot has sent messages here.'
            )
    except Exception as e:
        logger.error(f'Error in setup command: {e}', exc_info=True)
        try:
            await interaction.followup.send(
                'An error occurred during setup. Please check the bot logs and try again later.',
                ephemeral=True
            )
        except:
            await interaction.followup.send(
                'An error occurred during setup. Please check the bot logs and try again later.',
                ephemeral=True
            )


@bot.tree.command(name='ping', description='Check if the bot is responding')
async def ping(interaction: discord.Interaction):
    """Check if the bot is responding."""
    await interaction.response.send_message('Pong!')


@bot.tree.command(name='stats', description='Get Wordle statistics for a user (or yourself if no user specified)')
@app_commands.describe(user='The user to get statistics for (leave empty for your own stats)')
async def stats(interaction: discord.Interaction, user: Optional[discord.Member] = None):
    """
    Get Wordle statistics for a user.
    
    If no user is provided, returns stats for the command author.
    
    Args:
        interaction: Discord interaction object
        user: Optional Discord member to get stats for
    """
    if not interaction.guild:
        await interaction.response.send_message(
            'This command can only be used in a server. Please use it in a server channel.',
            ephemeral=True
        )
        return
    
    # Defer immediately to prevent "Unknown interaction" timeout
    await interaction.response.defer()
    
    try:
        # If no user provided, get stats for the command author
        if user is None:
            target_user_id = interaction.user.id
            user_stats = await get_user_stats_from_supabase(target_user_id)
            
            if not user_stats or target_user_id not in user_stats:
                await interaction.followup.send(
                    f'No statistics found. '
                    f'Make sure to have played Wordle within Discord and have run `/setup` first to collect statistics from Wordle Bot messages.',
                    ephemeral=True
                )
                return
            
            stats = user_stats[target_user_id]
            await interaction.followup.send(
                format_stats_message(stats, f'@{interaction.user.display_name}')
            )
        else:
            # Get stats for the specified user
            target_user_id = user.id
            user_stats = await get_user_stats_from_supabase(target_user_id)
            
            if not user_stats or target_user_id not in user_stats:
                await interaction.followup.send(
                    f'No statistics found. '
                    'They may not have played Wordle yet, or `/setup` has not been run to collect statistics.',
                    ephemeral=True
                )
                return
            
            stats = user_stats[target_user_id]
            await interaction.followup.send(
                format_stats_message(stats, f'@{user.display_name}')
            )
    except Exception as e:
        logger.error(f'Error in stats command: {e}', exc_info=True)
        await interaction.followup.send(
            'An error occurred while retrieving statistics. Please try again later.',
            ephemeral=True
        )


@bot.tree.command(name='help', description='Display information about all available commands')
async def help_command(interaction: discord.Interaction):
    """Display information about all available commands."""
    help_message = (
        '**Wordle Stats Bot - Command Help**\n\n'
        '📊 **Available Commands:**\n\n'
        '**`/stats [user]`**\n'
        'View Wordle statistics for yourself or another user.\n'
        '• `user`: (Optional) The user to view stats for. Defaults to you.\n\n'
        
        '**`/leaderboard [sort_by] [min_games]`**\n'
        'Display the server leaderboard.\n'
        '• `sort_by`: (Optional) Rank by Games Played (default), Win Rate, or Avg Guesses.\n'
        '• `min_games`: (Optional) Minimum games required to appear (default: 10).\n\n'
        
        '**`/setup`**\n'
        '(Admin Only) Scan the current channel for historical Wordle messages.\n'
        '• Requires `Manage Server` permission.\n'
        '• Use this to populate statistics from past games.\n\n'
        
        '**`/ping`**\n'
        'Check if the bot is responsive.\n\n'
        
        '**`/help`**\n'
        'Display this help message.\n\n'
        
        '💡 **Tip:** The bot automatically tracks new Wordle results posted in this channel!'
    )
    
    await interaction.response.send_message(help_message, ephemeral=True)


def format_stats_message(
    stats: Dict[str, Any],
    user_display: Any
) -> str:
    """
    Format user statistics into a readable message.
    
    Args:
        stats: Dictionary with user statistics
        user_display: String or member object to display for the user
        
    Returns:
        str: Formatted message
    """
    return (
        f'**Wordle Statistics for {user_display}**\n'
        f'📊 **Total Games**: {stats["total_games"]}\n'
        f'✅ **Wins**: {stats["wins"]} | '
        f'❌ **Losses**: {stats["losses"]}\n'
        f'📈 **Win Rate**: {stats["win_rate"]:.2f}% | '
        f'**Loss Rate**: {stats["loss_rate"]:.2f}%\n'
        f'🎯 **Average Guesses**: {stats["avg_guess"]:.2f}\n'
        f'🔢 **Total Guesses**: {stats["total_guesses"]}'
    )


@bot.tree.command(name='leaderboard', description='Display the Wordle leaderboard for this server')
@app_commands.describe(
    sort_by='How to rank players (default: most games played)',
    min_games='Minimum number of games played to appear on leaderboard (default: 10)'
)
@app_commands.choices(sort_by=[
    app_commands.Choice(name='Most Games Played', value='games'),
    app_commands.Choice(name='Highest Win Rate', value='win_rate'),
    app_commands.Choice(name='Lowest Average Guesses', value='avg_guess'),
])
async def leaderboard(
    interaction: discord.Interaction,
    sort_by: app_commands.Choice[str] = None,
    min_games: int = 10
):
    """
    Display the Wordle leaderboard for the server.
    
    Args:
        interaction: Discord interaction object
        sort_by: Optional choice for ranking criteria
        min_games: Minimum number of games played to appear on leaderboard (default: 10)
    """
    if not interaction.guild:
        await interaction.response.send_message(
            'This command can only be used in a server. Please use it in a server channel.',
            ephemeral=True
        )
        return
    
    # Defer immediately to prevent "Unknown interaction" timeout
    await interaction.response.defer()
    
    try:
        # Get member IDs from current guild
        member_ids = [member.id for member in interaction.guild.members]
        
        # Get user stats for guild members only (database-level filtering)
        all_stats = await get_user_stats_from_supabase(member_ids)
        
        if not all_stats:
            await interaction.followup.send(
                'No statistics found for this server. '
                'Run `/setup` first to collect statistics from Wordle Bot messages.',
                ephemeral=True
            )
            return
        
        # Determine sort criteria (default to 'games')
        sort_criteria = sort_by.value if sort_by else 'games'
        
        # Convert to list and filter by minimum games
        stats_list = list(all_stats.values())
        
        # Filter out users with less than min_games games played
        stats_list = [s for s in stats_list if s['total_games'] >= min_games]
        
        if sort_criteria == 'games':
            # Sort by most games played (descending)
            stats_list.sort(key=lambda x: x['total_games'], reverse=True)
            sort_display = 'Most Games Played'
        elif sort_criteria == 'win_rate':
            # Sort by highest win rate (descending)
            stats_list.sort(key=lambda x: x['win_rate'], reverse=True)
            sort_display = 'Highest Win Rate'
        elif sort_criteria == 'avg_guess':
            # Sort by lowest average guesses (ascending)
            stats_list.sort(key=lambda x: x['avg_guess'])
            sort_display = 'Lowest Average Guesses'
        else:
            # Fallback to games
            stats_list.sort(key=lambda x: x['total_games'], reverse=True)
            sort_display = 'Most Games Played'
        
        if not stats_list:
            await interaction.followup.send(
                'No statistics found for this server. Run `/setup` first to collect statistics.',
                ephemeral=True
            )
            return
        
        # Format leaderboard message
        min_games_text = f' (min {min_games} games)' if min_games > 0 else ''
        leaderboard_lines = [f'🏆 **Wordle Leaderboard** - Ranked by {sort_display}{min_games_text}\n']
        
        # Display top 20 players
        max_display = min(20, len(stats_list))
        medals = ['🥇', '🥈', '🥉']
        
        for i, stats in enumerate(stats_list[:max_display], 1):
            # Get member object if possible for proper mention
            member = interaction.guild.get_member(stats['user_id'])
            if member:
                user_display = f'@{member.display_name}'
            else:
                user_display = stats['username']
            
            # Medal for top 3
            rank_emoji = medals[i - 1] if i <= 3 else f'{i}.'
            
            if sort_criteria == 'games':
                value_display = f"{stats['total_games']} games"
            elif sort_criteria == 'win_rate':
                value_display = f"{stats['win_rate']:.2f}% win rate"
            elif sort_criteria == 'avg_guess':
                value_display = f"{stats['avg_guess']:.2f} avg guesses"
            else:
                value_display = f"{stats['total_games']} games"
            
            leaderboard_lines.append(
                f"{rank_emoji} {user_display} - {value_display} "
                f"({stats['wins']}W/{stats['losses']}L)"
            )
        
        if len(stats_list) > max_display:
            leaderboard_lines.append(
                f'\n*Showing top {max_display} of {len(stats_list)} players*'
            )
        
        leaderboard_message = '\n'.join(leaderboard_lines)
        
        # Discord message limit is 2000 characters
        if len(leaderboard_message) > 2000:
            # Truncate if needed
            leaderboard_message = leaderboard_message[:1997] + '...'
        
        await interaction.followup.send(leaderboard_message)
        
    except Exception as e:
        logger.error(
            f'Error in leaderboard command: {e}',
            exc_info=True
        )
        await interaction.followup.send(
            'An error occurred while retrieving the leaderboard. Please try again later.',
            ephemeral=True
        )


async def health_check_handler(request: web.Request) -> web.Response:
    """
    Simple health check handler for Cloud Run.
    
    Args:
        request: HTTP request object
        
    Returns:
        HTTP response with 200 OK status
    """
    return web.Response(text='OK', status=200)


async def start_http_server(port: int) -> web.Application:
    """
    Start the HTTP health check server.
    
    Args:
        port: Port number to listen on
        
    Returns:
        Web application instance
    """
    app = web.Application()
    app.router.add_get('/', health_check_handler)
    app.router.add_get('/health', health_check_handler)
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    
    logger.info(f'HTTP health check server started on port {port}')
    return app


async def main_async():
    """
    Main async function that starts both the HTTP server and Discord bot.
    """
    # Get port from environment variable (Cloud Run sets PORT, default to 8080)
    port = int(os.getenv('PORT', 8080))
    
    # Start HTTP health check server
    await start_http_server(port)
    
    # Start Discord bot
    discord_token = os.getenv('DISCORD_TOKEN')
    if not discord_token:
        logger.error('DISCORD_TOKEN environment variable not set')
        return
    
    await bot.start(discord_token)


def main():
    """
    Main entry point. Runs the async main function.
    """
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        logger.info('Bot stopped by user')
    except Exception as e:
        logger.error(f'Error starting bot: {e}', exc_info=True)
        raise

if __name__ == '__main__':
    main()


