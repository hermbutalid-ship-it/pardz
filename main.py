import discord
from discord.ext import commands
from discord import app_commands
import pandas as pd
import datetime
import io
import sqlite3
import os  # Added to load configuration from environment variables

# Initialize bot with required intents
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# Target the attached Railway persistent volume storage path
DB_FILE = "/data/orders.db"

def init_db():
    """Initializes the SQLite database and creates the orders table if it doesn't exist."""
    # Ensure the /data directory exists locally or in Railway volume mount
    os.makedirs(os.path.dirname(DB_FILE), exist_ok=True)
    
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS orders (
            order_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user TEXT NOT NULL,
            item TEXT NOT NULL,
            status TEXT NOT NULL,
            timestamp TEXT NOT NULL
        )
    ''')
    conn.commit()
    conn.close()

class OrderStatusView(discord.ui.View):
    """
    Persistent view containing buttons to update the status of an order.
    """
    def __init__(self, order_id: int):
        super().__init__(timeout=None)
        self.order_id = order_id
        self.clear_items()
        
        # Re-add buttons with order-specific custom IDs
        self.add_item(discord.ui.Button(label="Pending", style=discord.ButtonStyle.secondary, custom_id=f"btn_pending_{order_id}"))
        self.add_item(discord.ui.Button(label="On-Going", style=discord.ButtonStyle.primary, custom_id=f"btn_ongoing_{order_id}"))
        self.add_item(discord.ui.Button(label="Finish", style=discord.ButtonStyle.success, custom_id=f"btn_finish_{order_id}"))
        self.add_item(discord.ui.Button(label="Pickup", style=discord.ButtonStyle.danger, custom_id=f"btn_pickup_{order_id}"))

    async def handle_button_click(self, interaction: discord.Interaction, new_status: str, color_embed: discord.Color):
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        
        cursor.execute("SELECT order_id FROM orders WHERE order_id = ?", (self.order_id,))
        if not cursor.fetchone():
            await interaction.response.send_message("This order no longer exists in the database.", ephemeral=True)
            conn.close()
            return

        cursor.execute("UPDATE orders SET status = ? WHERE order_id = ?", (new_status, self.order_id))
        conn.commit()
        conn.close()
        
        embed = interaction.message.embeds[0]
        embed.color = color_embed
        
        for i, field in enumerate(embed.fields):
            if field.name == "Status":
                embed.set_field_at(i, name="Status", value=f"**{new_status}**", inline=True)
                break
                
        await interaction.response.edit_message(embed=embed, view=self)

@bot.event
async def on_ready():
    init_db()
    print(f"Logged in as {bot.user.name} (ID: {bot.user.id})")
    bot.add_view(discord.ui.View(timeout=None)) 
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} application command(s).")
    except Exception as e:
        print(f"Failed to sync commands: {e}")

@bot.event
async def on_interaction(interaction: discord.Interaction):
    if interaction.type == discord.InteractionType.component:
        custom_id = interaction.data.get("custom_id", "")
        if custom_id.startswith(("btn_pending_", "btn_ongoing_", "btn_finish_", "btn_pickup_")):
            parts = custom_id.split("_")
            status_type = parts[1]
            order_id = int(parts[2])
            
            status_map = {
                "pending": ("Pending", discord.Color.light_grey()),
                "ongoing": ("On-Going", discord.Color.blue()),
                "finish": ("Finish", discord.Color.green()),
                "pickup": ("Pickup", discord.Color.red())
            }
            
            status_text, color = status_map[status_type]
            view = OrderStatusView(order_id)
            await view.handle_button_click(interaction, status_text, color)

@bot.tree.command(name="add_order", description="Add a new item order to the lobby.")
@app_commands.describe(item="The item details or description you want to order")
async def add_order(interaction: discord.Interaction, item: str):
    timestamp_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    user_name = str(interaction.user)
    
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO orders (user, item, status, timestamp) VALUES (?, ?, ?, ?)",
        (user_name, item, "Pending", timestamp_str)
    )
    order_id = cursor.lastrowid
    conn.commit()
    conn.close()
    
    embed = discord.Embed(
        title=f"📋 Order #{order_id}", 
        description=f"New order added to the lobby.", 
        color=discord.Color.light_grey()
    )
    embed.add_field(name="Customer", value=interaction.user.mention, inline=True)
    embed.add_field(name="Status", value="**Pending**", inline=True)
    embed.add_field(name="Ordered Item", value=item, inline=False)
    embed.set_footer(text=f"Placed at {timestamp_str}")
    
    view = OrderStatusView(order_id)
    await interaction.response.send_message(embed=embed, view=view)

@bot.tree.command(name="export_orders", description="Export all logged lobby orders directly to an Excel file.")
async def export_orders(interaction: discord.Interaction):
    conn = sqlite3.connect(DB_FILE)
    df = pd.read_sql_query("SELECT order_id AS 'Order ID', user AS 'Customer Username', item AS 'Ordered Item', status AS 'Current Status', timestamp AS 'Timestamp Created' FROM orders", conn)
    conn.close()

    if df.empty:
        await interaction.response.send_message("There are currently no orders in the database registry to export.", ephemeral=True)
        return
        
    with io.BytesIO() as excel_binary:
        with pd.ExcelWriter(excel_binary, engine='openpyxl') as writer:
            df.to_excel(writer, index=False, sheet_name='Lobby Orders Registry')
        excel_binary.seek(0)
        
        discord_file = discord.File(fp=excel_binary, filename=f"lobby_orders_{datetime.date.today()}.xlsx")
        await interaction.response.send_message("Here is the requested data spreadsheet:", file=discord_file)

# Safe environmental variable check for Railway deployment
token = os.environ.get("DISCORD_TOKEN")
if token:
    bot.run(token)
else:
    print("CRITICAL ERROR: 'DISCORD_TOKEN' environment variable is missing!")
