import discord
from discord.ext import commands
from discord import app_commands
import pandas as pd
import datetime
import io
import sqlite3

# Initialize bot with required intents
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# Change this line in your main.py file
DB_FILE = "/data/orders.db"

def init_db():
    """Initializes the SQLite database and creates the orders table if it doesn't exist."""
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
        # Setting custom_ids dynamically ensures the view can be re-registered on bot restart
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
        
        # Check if order exists
        cursor.execute("SELECT order_id FROM orders WHERE order_id = ?", (self.order_id,))
        if not cursor.fetchone():
            await interaction.response.send_message("This order no longer exists in the database.", ephemeral=True)
            conn.close()
            return

        # Update status in SQLite
        cursor.execute("UPDATE orders SET status = ? WHERE order_id = ?", (new_status, self.order_id))
        conn.commit()
        conn.close()
        
        # Edit the original embed to reflect the changes visually
        embed = interaction.message.embeds[0]
        embed.color = color_embed
        
        # Find and update the Status field in the embed
        for i, field in enumerate(embed.fields):
            if field.name == "Status":
                embed.set_field_at(i, name="Status", value=f"**{new_status}**", inline=True)
                break
                
        await interaction.response.edit_message(embed=embed, view=self)

@bot.event
async def on_ready():
    init_db()  # Setup database tables
    print(f"Logged in as {bot.user.name} (ID: {bot.user.id})")
    
    # We must listen to raw interactions to handle old buttons after a reboot
    bot.add_view(discord.ui.View(timeout=None)) 
    
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} application command(s).")
    except Exception as e:
        print(f"Failed to sync commands: {e}")

@bot.event
async def on_interaction(interaction: discord.Interaction):
    """Global handler to catch persistent button clicks across bot restarts."""
    if interaction.type == discord.InteractionType.component:
        custom_id = interaction.data.get("custom_id", "")
        if custom_id.startswith(("btn_pending_", "btn_ongoing_", "btn_finish_", "btn_pickup_")):
            # Extract status type and order ID from custom_id
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
    
    # Insert order into database and retrieve the auto-generated ID
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO orders (user, item, status, timestamp) VALUES (?, ?, ?, ?)",
        (user_name, item, "Pending", timestamp_str)
    )
    order_id = cursor.lastrowid
    conn.commit()
    conn.close()
    
    # Construct an interactive embed layout
    embed = discord.Embed(
        title=f"📋 Order #{order_id}", 
        description=f"New order added to the lobby.", 
        color=discord.Color.light_grey()
    )
    embed.add_field(name="Customer", value=interaction.user.mention, inline=True)
    embed.add_field(name="Status", value="**Pending**", inline=True)
    embed.add_field(name="Ordered Item", value=item, inline=False)
    embed.set_footer(text=f"Placed at {timestamp_str}")
    
    # Attach our status adjustment view with buttons
    view = OrderStatusView(order_id)
    await interaction.response.send_message(embed=embed, view=view)


@bot.tree.command(name="export_orders", description="Export all logged lobby orders directly to an Excel file.")
async def export_orders(interaction: discord.Interaction):
    conn = sqlite3.connect(DB_FILE)
    # Pull directly from SQLite table into a Pandas DataFrame
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
        await interaction.response.send_message("Here is the requested permanent data sheet spreadsheet:", file=discord_file)


# Paste your unique Discord Bot Application Token below to launch 
# bot.run("YOUR_DISCORD_BOT_TOKEN")
