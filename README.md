# Wordle Stats Bot

A Discord bot that tracks and displays Wordle statistics for your server. The bot automatically processes Wordle results shared by the official Wordle bot and maintains detailed statistics including win rates, average guesses, and game history.

## Features

- **Automatic Statistics Tracking**: Monitors channels for Wordle results and automatically updates user statistics.
- **Slash Commands**: Modern Discord slash command interface for easy interaction.
- **Comprehensive Stats**: Track total games, wins, losses, win rate, and average guess count.
- **Leaderboards**: View server rankings sorted by games played, win rate, or average guesses.
- **Persistent Storage**: Integrates with Supabase for reliable data persistence.
- **Historical Processing**: Can scan channel history to collect past Wordle results.
- **Race Condition Protection**: Handles concurrent messages safely with per-guild locking.
- **Health Check**: Built-in HTTP server for health monitoring (useful for container deployments).

## Commands

| Command | Description |
|---------|-------------|
| `/stats [user]` | View Wordle statistics for yourself or another user. |
| `/leaderboard` | Display the server leaderboard. Options to sort by games, win rate, or average guesses. |
| `/setup` | (Admin only) Scan the current channel for historical Wordle messages to populate the database. |
| `/ping` | Check if the bot is responsive. |
| `/help` | Display information about available commands. |

## Installation

### Prerequisites

- Python 3.11+ (for local setup) or Docker (for containerized setup)
- A Discord Bot Token (from [Discord Developer Portal](https://discord.com/developers/applications))
- (Optional) A Supabase project for database storage

### Local Setup

1. **Clone the repository:**
   ```bash
   git clone https://github.com/jhyang21/wordlestatsbot.git
   cd wordlestatsbot
   ```

2. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

3. **Configure environment variables:**
   Create a `.env` file in the root directory:
   ```env
   DISCORD_TOKEN=your_discord_bot_token
   SUPABASE_URL=your_supabase_project_url
   SUPABASE_KEY=your_supabase_anon_key
   ```

4. **Run the bot:**
   ```bash
   python bot.py
   ```

### Docker Setup

#### Using Docker Compose (Recommended)

1. **Clone the repository:**
   ```bash
   git clone https://github.com/jhyang21/wordlestatsbot.git
   cd wordlestatsbot
   ```

2. **Configure environment variables:**
   Create a `.env` file in the root directory:
   ```env
   DISCORD_TOKEN=your_discord_bot_token
   SUPABASE_URL=your_supabase_project_url
   SUPABASE_KEY=your_supabase_anon_key
   ```

3. **Build and run with Docker Compose:**
   ```bash
   docker-compose up -d
   ```

   To view logs:
   ```bash
   docker-compose logs -f
   ```

   To stop the bot:
   ```bash
   docker-compose down
   ```

#### Using Docker directly

1. **Build the image:**
   ```bash
   docker build -t wordlestatsbot .
   ```

2. **Run the container:**
   ```bash
   docker run -d \
     --name wordlestatsbot \
     --restart unless-stopped \
     --env-file .env \
     -v $(pwd)/logs:/app/logs \
     wordlestatsbot
   ```

   Note: On Windows PowerShell, use `%cd%` instead of `$(pwd)`:
   ```powershell
   docker run -d `
     --name wordlestatsbot `
     --restart unless-stopped `
     --env-file .env `
     -v ${PWD}/logs:/app/logs `
     wordlestatsbot
   ```

## Deployment

The bot includes a health check server on port 8080 (configurable via `PORT` env var) to make it compatible with container platforms that require a listening port.

Ensure you set the required environment variables in your deployment environment.

## Database Schema (Supabase)

If using Supabase, create a table named `user_stats` with the following schema:

```sql
create table public.user_stats (
  id uuid not null default gen_random_uuid (),
  user_id text not null,
  username text null,
  total_games integer null default 0,
  total_guesses integer null default 0,
  wins integer null default 0,
  losses integer null default 0,
  win_rate numeric null,
  loss_rate numeric null,
  avg_guess numeric null,
  constraint user_stats_pkey primary key (id),
  constraint user_stats_user_id_key unique (user_id)
);
```

## Logging

Logs are stored in the `logs/` directory. The main log file is `wordlestatsbot.log`. The bot uses rotating file handlers to manage log size.

## Troubleshooting

- **Bot not responding to slash commands?**
  - Ensure the bot is invited with the `applications.commands` scope.
  - If you just started the bot, global command syncing can take up to an hour.

- **Stats not updating?**
  - Ensure the bot has "Read Messages" and "Read Message History" permissions in the channel where Wordle results are posted.
  - The bot specifically looks for messages from the Wordle bot or messages matching the Wordle result pattern.

## License

[MIT License](LICENSE)
