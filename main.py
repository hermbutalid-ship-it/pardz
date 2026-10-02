import discord
from discord.ext import commands
from discord import app_commands
import pandas as pd
import datetime
import io
import sqlite3
import os


# Initialize bot with required intents
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# Target the attached Railway persistent volume storage path
DB_FILE = "/data/orders.db"


def init_db():
    """Initializes the SQLite database and creates the necessary tables."""
    os.makedirs(os.path.dirname(DB_FILE), exist_ok=True)
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    # Core orders table with customer and price columns added
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS orders (
            order_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user TEXT NOT NULL,
            customer TEXT NOT NULL,
            item TEXT NOT NULL,
            price TEXT NOT NULL,
            status TEXT NOT NULL,
            timestamp TEXT NOT NULL
        )
        """
    )

    # Configuration table to hold the logging channel per server
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS bot_config (
            guild_id INTEGER PRIMARY KEY,
            logging_channel_id INTEGER NOT NULL
        )
        """
    )

    # Dedicated channel where every new order gets a follow-up card/button.
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS follow_up_config (
            guild_id INTEGER PRIMARY KEY,
            follow_up_channel_id INTEGER NOT NULL
        )
        """
    )

    conn.commit()
    conn.close()


def get_logging_channel_id(guild_id: int):
    """Retrieves the configured logging channel ID for a specific guild."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT logging_channel_id FROM bot_config WHERE guild_id = ?",
        (guild_id,),
    )
    result = cursor.fetchone()
    conn.close()
    return result[0] if result else None


def get_follow_up_channel_id(guild_id: int):
    """Retrieves the configured follow-up channel ID for a specific guild."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT follow_up_channel_id FROM follow_up_config WHERE guild_id = ?",
        (guild_id,),
    )
    result = cursor.fetchone()
    conn.close()
    return result[0] if result else None


class FollowUpView(discord.ui.View):
    """Persistent view containing the Follow Up button for an order."""

    def __init__(self, order_id: int):
        super().__init__(timeout=None)
        self.order_id = order_id

        self.add_item(
            discord.ui.Button(
                label="Follow Up",
                emoji="📞",
                style=discord.ButtonStyle.primary,
                custom_id=f"btn_followup_{order_id}",
            )
        )


    async def handle_button_click(self, interaction: discord.Interaction):
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()

        cursor.execute(
            "SELECT customer, item, price, status FROM orders WHERE order_id = ?",
            (self.order_id,),
        )
        order_data = cursor.fetchone()

        conn.close()

        if not order_data:
            await interaction.response.send_message(
                "❌ This order no longer exists in the database.",
                ephemeral=True,
            )
            return

        customer, item_details, price, status = order_data

        # Acknowledge the button immediately.
        await interaction.response.send_message(
            f"📞 Follow-up recorded for **Order #{self.order_id}** — **{customer}**.",
            ephemeral=True,
        )

        # Update the follow-up card so staff can see that it was actioned.
        try:
            if interaction.message and interaction.message.embeds:
                embed = interaction.message.embeds[0]
                embed.set_footer(
                    text=f"Last followed up by {interaction.user} • "
                         f"{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                )

                updated_view = FollowUpView(self.order_id)
                button = updated_view.children[0]
                button.label = "Followed Up"
                button.emoji = "✅"
                button.style = discord.ButtonStyle.success

                await interaction.message.edit(embed=embed, view=updated_view)
        except Exception as e:
            print(f"Could not update follow-up card: {e}")

        # Send the follow-up alert to the configured logging channel.
        log_channel_id = get_logging_channel_id(interaction.guild_id)

        if log_channel_id:
            log_channel = interaction.guild.get_channel(log_channel_id)

            if log_channel:
                log_embed = discord.Embed(
                    title="📞 Order Follow-Up",
                    color=discord.Color.orange(),
                    timestamp=datetime.datetime.now(),
                )

                log_embed.add_field(
                    name="Order ID",
                    value=f"#{self.order_id}",
                    inline=True,
                )
                log_embed.add_field(
                    name="Customer",
                    value=customer,
                    inline=True,
                )
                log_embed.add_field(
                    name="Followed Up By",
                    value=interaction.user.mention,
                    inline=True,
                )
                log_embed.add_field(
                    name="Current Status",
                    value=status,
                    inline=True,
                )
                log_embed.add_field(
                    name="Item Details",
                    value=item_details,
                    inline=True,
                )
                log_embed.add_field(
                    name="Price",
                    value=price,
                    inline=True,
                )

                await log_channel.send(
                    content=f"📢 **Follow-up alert:** {customer}",
                    embed=log_embed,
                )


class OrderStatusView(discord.ui.View):
    """
    Persistent view containing buttons to update the status of an order.
    """

    def __init__(self, order_id: int):
        super().__init__(timeout=None)
        self.order_id = order_id
        self.clear_items()

        self.add_item(
            discord.ui.Button(
                label="Pending",
                style=discord.ButtonStyle.secondary,
                custom_id=f"btn_pending_{order_id}",
            )
        )
        self.add_item(
            discord.ui.Button(
                label="On-Going",
                style=discord.ButtonStyle.primary,
                custom_id=f"btn_ongoing_{order_id}",
            )
        )
        self.add_item(
            discord.ui.Button(
                label="Finish",
                style=discord.ButtonStyle.success,
                custom_id=f"btn_finish_{order_id}",
            )
        )
        self.add_item(
            discord.ui.Button(
                label="Pickup",
                style=discord.ButtonStyle.danger,
                custom_id=f"btn_pickup_{order_id}",
            )
        )

    async def handle_button_click(
        self,
        interaction: discord.Interaction,
        new_status: str,
        color_embed: discord.Color,
    ):
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()

        # Verify the order exists
        cursor.execute(
            "SELECT customer, item, price, status FROM orders WHERE order_id = ?",
            (self.order_id,),
        )
        order_data = cursor.fetchone()

        if not order_data:
            await interaction.response.send_message(
                "This order no longer exists in the database.",
                ephemeral=True,
            )
            conn.close()
            return

        customer, item_details, price, old_status = order_data

        # Protect against duplicate double clicks
        if old_status == new_status:
            await interaction.response.send_message(
                f"This order is already marked as {new_status}.",
                ephemeral=True,
            )
            conn.close()
            return

        # Update status in database
        cursor.execute(
            "UPDATE orders SET status = ? WHERE order_id = ?",
            (new_status, self.order_id),
        )
        conn.commit()
        conn.close()

        # Edit the original lobby embed to update visually
        embed = interaction.message.embeds[0]
        embed.color = color_embed

        for i, field in enumerate(embed.fields):
            if field.name == "Status":
                embed.set_field_at(
                    i,
                    name="Status",
                    value=f"**{new_status}**",
                    inline=True,
                )
                break

        await interaction.response.edit_message(embed=embed, view=self)

        # Send automatic broadcast to logging channel if configured
        log_channel_id = get_logging_channel_id(interaction.guild_id)

        if log_channel_id:
            log_channel = interaction.guild.get_channel(log_channel_id)

            if log_channel:
                log_embed = discord.Embed(
                    title="📈 Order Status Updated",
                    color=color_embed,
                    timestamp=datetime.datetime.now(),
                )

                log_embed.add_field(
                    name="Order ID",
                    value=f"#{self.order_id}",
                    inline=True,
                )
                log_embed.add_field(
                    name="Updated By",
                    value=interaction.user.mention,
                    inline=True,
                )
                log_embed.add_field(
                    name="Status Transition",
                    value=f"`{old_status}` ➡️ **{new_status}**",
                    inline=False,
                )
                log_embed.add_field(
                    name="Customer",
                    value=customer,
                    inline=True,
                )
                log_embed.add_field(
                    name="Item Details",
                    value=item_details,
                    inline=True,
                )
                log_embed.add_field(
                    name="Price",
                    value=price,
                    inline=True,
                )

                await log_channel.send(embed=log_embed)


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

        if custom_id.startswith("btn_followup_"):
            order_id = int(custom_id.split("_")[2])
            view = FollowUpView(order_id)
            await view.handle_button_click(interaction)
            return

        if custom_id.startswith(
            ("btn_pending_", "btn_ongoing_", "btn_finish_", "btn_pickup_")
        ):
            parts = custom_id.split("_")
            status_type = parts[1]
            order_id = int(parts[2])

            status_map = {
                "pending": ("Pending", discord.Color.light_grey()),
                "ongoing": ("On-Going", discord.Color.blue()),
                "finish": ("Finish", discord.Color.green()),
                "pickup": ("Pickup", discord.Color.red()),
            }

            status_text, color = status_map[status_type]
            view = OrderStatusView(order_id)
            await view.handle_button_click(interaction, status_text, color)


@bot.tree.command(
    name="add_order",
    description="Add a new item order to the lobby.",
)
@app_commands.describe(
    item="The item details or description you want to order",
    customer="Name or tag of the customer (Optional)",
    price="The cost of the item (Optional)",
)
async def add_order(
    interaction: discord.Interaction,
    item: str,
    customer: str = None,
    price: str = None,
):
    timestamp_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    user_name = str(interaction.user)

    # Handle defaults if parameters are omitted or left blank
    final_customer = customer if customer else "Not Provided"
    final_price = price if price else "0"

    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    cursor.execute(
        """
        INSERT INTO orders
        (user, customer, item, price, status, timestamp)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            user_name,
            final_customer,
            item,
            final_price,
            "Pending",
            timestamp_str,
        ),
    )

    order_id = cursor.lastrowid
    conn.commit()
    conn.close()

    embed = discord.Embed(
        title=f"📋 Order #{order_id}",
        description="New order added to the lobby.",
        color=discord.Color.light_grey(),
    )

    embed.add_field(
        name="Customer",
        value=final_customer,
        inline=True,
    )
    embed.add_field(
        name="Price",
        value=final_price,
        inline=True,
    )
    embed.add_field(
        name="Status",
        value="**Pending**",
        inline=True,
    )
    embed.add_field(
        name="Ordered Item",
        value=item,
        inline=False,
    )
    embed.set_footer(text=f"Logged by {user_name} at {timestamp_str}")

    view = OrderStatusView(order_id)
    await interaction.response.send_message(embed=embed, view=view)

    # Also publish a dedicated follow-up card in the configured Follow Up channel.
    follow_up_channel_id = get_follow_up_channel_id(interaction.guild_id)

    if follow_up_channel_id:
        follow_up_channel = interaction.guild.get_channel(follow_up_channel_id)

        if follow_up_channel:
            follow_up_embed = discord.Embed(
                title=f"📞 Follow Up — Order #{order_id}",
                description="A new order requires customer follow-up.",
                color=discord.Color.orange(),
            )

            # Customer name is prominently shown beside the order information.
            follow_up_embed.add_field(
                name="👤 Customer",
                value=f"**{final_customer}**",
                inline=True,
            )
            follow_up_embed.add_field(
                name="🧾 Order",
                value=f"**#{order_id}**",
                inline=True,
            )
            follow_up_embed.add_field(
                name="💰 Price",
                value=final_price,
                inline=True,
            )
            follow_up_embed.add_field(
                name="📦 Ordered Item",
                value=item,
                inline=False,
            )
            follow_up_embed.add_field(
                name="📊 Status",
                value="**Pending**",
                inline=True,
            )
            follow_up_embed.set_footer(
                text=f"Created by {user_name} • {timestamp_str}"
            )

            await follow_up_channel.send(
                content=f"🔔 **New order follow-up:** {final_customer}",
                embed=follow_up_embed,
                view=FollowUpView(order_id),
            )
        else:
            print(
                f"Follow-up channel {follow_up_channel_id} could not be found "
                f"in guild {interaction.guild_id}."
            )


@bot.tree.command(
    name="edit_order",
    description="Edit missing details or correct items on an existing order.",
)
@app_commands.describe(
    order_id="The numeric ID of the order you want to update",
    item="Update the item description (Optional)",
    customer="Update the customer name/tag (Optional)",
    price="Update the item price (Optional)",
)
@app_commands.checks.has_permissions(administrator=True)
async def edit_order(
    interaction: discord.Interaction,
    order_id: int,
    item: str = None,
    customer: str = None,
    price: str = None,
):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    # Fetch existing data first
    cursor.execute(
        "SELECT item, customer, price FROM orders WHERE order_id = ?",
        (order_id,),
    )
    existing = cursor.fetchone()

    if not existing:
        await interaction.response.send_message(
            f"❌ Order #{order_id} could not be found.",
            ephemeral=True,
        )
        conn.close()
        return

    # Retain old value if the update option is not provided
    updated_item = item if item else existing[0]
    updated_customer = customer if customer else existing[1]
    updated_price = price if price else existing[2]

    cursor.execute(
        """
        UPDATE orders
        SET item = ?, customer = ?, price = ?
        WHERE order_id = ?
        """,
        (
            updated_item,
            updated_customer,
            updated_price,
            order_id,
        ),
    )

    conn.commit()
    conn.close()

    await interaction.response.send_message(
        f"✅ **Order #{order_id} successfully updated!**\n"
        f"• **Customer:** {updated_customer}\n"
        f"• **Item:** {updated_item}\n"
        f"• **Price:** {updated_price}"
    )


@bot.tree.command(
    name="set_logging_channel",
    description="Configure where order status modifications will be logged.",
)
@app_commands.describe(channel="The text channel to send logs to")
@app_commands.checks.has_permissions(administrator=True)
async def set_logging_channel(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    cursor.execute(
        """
        INSERT INTO bot_config (guild_id, logging_channel_id)
        VALUES (?, ?)
        ON CONFLICT(guild_id)
        DO UPDATE SET logging_channel_id = excluded.logging_channel_id
        """,
        (interaction.guild_id, channel.id),
    )

    conn.commit()
    conn.close()

    await interaction.response.send_message(
        f"✅ Status update alerts will now be logged automatically in {channel.mention}."
    )


@bot.tree.command(
    name="set_follow_up_channel",
    description="Configure where new order follow-up cards will be posted.",
)
@app_commands.describe(channel="The text channel to receive new order follow-up cards")
@app_commands.checks.has_permissions(administrator=True)
async def set_follow_up_channel(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    cursor.execute(
        """
        INSERT INTO follow_up_config (guild_id, follow_up_channel_id)
        VALUES (?, ?)
        ON CONFLICT(guild_id)
        DO UPDATE SET follow_up_channel_id = excluded.follow_up_channel_id
        """,
        (interaction.guild_id, channel.id),
    )

    conn.commit()
    conn.close()

    await interaction.response.send_message(
        f"✅ New order follow-up cards will now be posted automatically in {channel.mention}."
    )


@bot.tree.command(
    name="delete_order",
    description="Remove an order completely from the lobby records.",
)
@app_commands.describe(
    order_id="The numeric ID of the order you want to delete"
)
@app_commands.checks.has_permissions(administrator=True)
async def delete_order(
    interaction: discord.Interaction,
    order_id: int,
):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    cursor.execute(
        "SELECT order_id FROM orders WHERE order_id = ?",
        (order_id,),
    )
    order = cursor.fetchone()

    if not order:
        await interaction.response.send_message(
            f"❌ Order #{order_id} could not be found in the system.",
            ephemeral=True,
        )
        conn.close()
        return

    cursor.execute(
        "DELETE FROM orders WHERE order_id = ?",
        (order_id,),
    )

    conn.commit()
    conn.close()

    await interaction.response.send_message(
        f"🗑️ Order #{order_id} has been permanently deleted from the database."
    )


@bot.tree.command(
    name="export_orders",
    description="Export all logged lobby orders directly to an Excel file.",
)
@app_commands.checks.has_permissions(administrator=True)
async def export_orders(interaction: discord.Interaction):
    conn = sqlite3.connect(DB_FILE)

    # Pull data including new customer and price metrics into the Excel dataframe
    df = pd.read_sql_query(
        """
        SELECT
            order_id AS 'Order ID',
            user AS 'Logged By',
            customer AS 'Customer Name/Tag',
            item AS 'Ordered Item',
            price AS 'Price',
            status AS 'Current Status',
            timestamp AS 'Timestamp Created'
        FROM orders
        """,
        conn,
    )

    conn.close()

    if df.empty:
        await interaction.response.send_message(
            "There are currently no orders in the database registry to export.",
            ephemeral=True,
        )
        return

    with io.BytesIO() as excel_binary:
        with pd.ExcelWriter(
            excel_binary,
            engine="openpyxl",
        ) as writer:
            df.to_excel(
                writer,
                index=False,
                sheet_name="Lobby Orders Registry",
            )

        excel_binary.seek(0)

        discord_file = discord.File(
            fp=excel_binary,
            filename=f"lobby_orders_{datetime.date.today()}.xlsx",
        )

        await interaction.response.send_message(
            "Here is the requested data spreadsheet containing comprehensive parameters:",
            file=discord_file,
        )


@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message(
            "⚠️ You do not have the Administrator permissions required to run this command.",
            ephemeral=True,
        )
    else:
        raise error


# Safe environmental variable check for Railway deployment
token = os.environ.get("DISCORD_TOKEN")

if token:
    bot.run(token)
else:
    print("CRITICAL ERROR: 'DISCORD_TOKEN' environment variable is missing!")
