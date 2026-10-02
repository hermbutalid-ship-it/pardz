import discord
from discord.ext import commands
from discord import app_commands
import pandas as pd
import datetime
import io
import sqlite3
import os
from decimal import Decimal, InvalidOperation


# Initialize bot with required intents
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# Target the attached Railway persistent volume storage path
DB_FILE = "/data/orders.db"


def parse_price(value: str) -> Decimal:
    """Parse a price such as 250, 250.50, ₱250, or $250.50."""
    cleaned = str(value).strip().replace(",", "")
    for symbol in ("₱", "$", "€", "£"):
        cleaned = cleaned.replace(symbol, "")
    if not cleaned:
        return Decimal("0")
    try:
        amount = Decimal(cleaned)
    except InvalidOperation as exc:
        raise ValueError("Price must be a valid number.") from exc
    if amount < 0:
        raise ValueError("Price cannot be negative.")
    return amount


def format_price(value) -> str:
    """Format a numeric price with two decimal places."""
    amount = Decimal(str(value))
    return f"{amount:,.2f}"


def calculate_total_price(unit_price: str, quantity: int) -> Decimal:
    return parse_price(unit_price) * Decimal(quantity)


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
            quantity INTEGER NOT NULL DEFAULT 1,
            price TEXT NOT NULL,
            total_price TEXT NOT NULL DEFAULT '0',
            status TEXT NOT NULL,
            timestamp TEXT NOT NULL
        )
        """
    )

    # Add quantity to older databases that were created before quantity existed.
    cursor.execute("PRAGMA table_info(orders)")
    order_columns = {row[1] for row in cursor.fetchall()}
    if "quantity" not in order_columns:
        cursor.execute("ALTER TABLE orders ADD COLUMN quantity INTEGER NOT NULL DEFAULT 1")
    if "total_price" not in order_columns:
        cursor.execute("ALTER TABLE orders ADD COLUMN total_price TEXT NOT NULL DEFAULT '0'")
        cursor.execute("UPDATE orders SET total_price = price * quantity")

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

    # Configuration table for the channel where photo approvals are posted.
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS approval_config (
            guild_id INTEGER PRIMARY KEY,
            approval_channel_id INTEGER NOT NULL
        )
        """
    )

    # Photo approval requests.
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS approval_requests (
            approval_id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            creator_id INTEGER NOT NULL,
            creator_name TEXT NOT NULL,
            customer TEXT NOT NULL,
            photo_filename TEXT NOT NULL,
            status TEXT NOT NULL,
            message_id INTEGER,
            channel_id INTEGER,
            timestamp TEXT NOT NULL,
            rejection_reason TEXT
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


def get_approval_channel_id(guild_id: int):
    """Retrieves the configured photo approval channel ID for a guild."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT approval_channel_id FROM approval_config WHERE guild_id = ?",
        (guild_id,),
    )
    result = cursor.fetchone()
    conn.close()
    return result[0] if result else None


class ApprovalRejectModal(discord.ui.Modal):
    """Modal used to collect the reason when an approval is rejected."""

    def __init__(self, approval_id: int):
        super().__init__(title="Reject Photo Approval")
        self.approval_id = approval_id

        self.reason = discord.ui.TextInput(
            label="Reason for rejection",
            placeholder="Enter the reason that should be sent to the member...",
            style=discord.TextStyle.paragraph,
            required=True,
            min_length=1,
            max_length=1000,
        )
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction):
        reason = str(self.reason.value).strip()

        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT creator_id, customer, status, message_id, channel_id
            FROM approval_requests
            WHERE approval_id = ?
            """,
            (self.approval_id,),
        )
        approval = cursor.fetchone()

        if not approval:
            conn.close()
            await interaction.response.send_message(
                "❌ This approval request no longer exists.",
                ephemeral=True,
            )
            return

        creator_id, customer, status, message_id, channel_id = approval

        if status != "Pending":
            conn.close()
            await interaction.response.send_message(
                f"⚠️ This request has already been processed as **{status}**.",
                ephemeral=True,
            )
            return

        cursor.execute(
            """
            UPDATE approval_requests
            SET status = ?, rejection_reason = ?
            WHERE approval_id = ?
            """,
            ("Rejected", reason, self.approval_id),
        )
        conn.commit()
        conn.close()

        # Acknowledge the modal first so Discord does not time out.
        await interaction.response.defer(ephemeral=True)

        # Delete the approval message. This also removes the uploaded photo
        # from the to-be-approved channel.
        deleted = False
        try:
            if interaction.message:
                await interaction.message.delete()
                deleted = True
            elif channel_id and message_id:
                channel = interaction.guild.get_channel(channel_id)
                if channel:
                    message = await channel.fetch_message(message_id)
                    await message.delete()
                    deleted = True
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
            print(f"Could not delete rejected approval message #{self.approval_id}: {e}")

        # DM the member who originally used /approval.
        dm_sent = False
        try:
            creator = bot.get_user(creator_id) or await bot.fetch_user(creator_id)
            await creator.send(
                f"❌ Your photo approval for **{customer}** was rejected.\n"
                f"**Reason:** {reason}"
            )
            dm_sent = True
        except (discord.Forbidden, discord.HTTPException) as e:
            print(f"Could not DM approval creator {creator_id}: {e}")

        result = "❌ Rejected."
        if not deleted:
            result += " I could not delete the approval message."
        if not dm_sent:
            result += " I could not send the creator a DM."

        await interaction.followup.send(result, ephemeral=True)


class ApprovalView(discord.ui.View):
    """Check/X buttons shown with every photo approval request."""

    def __init__(self, approval_id: int, disabled: bool = False):
        super().__init__(timeout=None)
        self.approval_id = approval_id

        check_button = discord.ui.Button(
            label="Approve",
            emoji="✅",
            style=discord.ButtonStyle.success,
            custom_id=f"approval_check_{approval_id}",
            disabled=disabled,
        )
        x_button = discord.ui.Button(
            label="Reject",
            emoji="❌",
            style=discord.ButtonStyle.danger,
            custom_id=f"approval_reject_{approval_id}",
            disabled=disabled,
        )

        self.add_item(check_button)
        self.add_item(x_button)


async def handle_approval_check(interaction: discord.Interaction, approval_id: int):
    """Approve an approval request and keep its photo in the channel."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT creator_id, customer, status
        FROM approval_requests
        WHERE approval_id = ?
        """,
        (approval_id,),
    )
    approval = cursor.fetchone()

    if not approval:
        conn.close()
        await interaction.response.send_message(
            "❌ This approval request no longer exists.",
            ephemeral=True,
        )
        return

    creator_id, customer, status = approval

    if status != "Pending":
        conn.close()
        await interaction.response.send_message(
            f"⚠️ This request has already been processed as **{status}**.",
            ephemeral=True,
        )
        return

    cursor.execute(
        """
        UPDATE approval_requests
        SET status = ?
        WHERE approval_id = ?
        """,
        ("Approved", approval_id),
    )
    conn.commit()
    conn.close()

    # Keep the message/photo in the channel, but disable the buttons.
    if interaction.message:
        try:
            embed = interaction.message.embeds[0] if interaction.message.embeds else None
            if embed:
                embed.color = discord.Color.green()
                embed.set_footer(
                    text=f"Approved by {interaction.user} • "
                         f"{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                )

            await interaction.response.edit_message(
                embed=embed,
                view=ApprovalView(approval_id, disabled=True),
            )
        except discord.HTTPException:
            await interaction.response.send_message(
                "✅ Approved, but I could not update the approval message.",
                ephemeral=True,
            )
    else:
        await interaction.response.send_message(
            "✅ Approved.",
            ephemeral=True,
        )

    # DM the member who originally used /approval.
    try:
        creator = bot.get_user(creator_id) or await bot.fetch_user(creator_id)
        await creator.send(
            f"✅ Your photo approval for **{customer}** has been approved by "
            f"**{interaction.user}**."
        )
    except (discord.Forbidden, discord.HTTPException) as e:
        print(f"Could not DM approval creator {creator_id}: {e}")


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
            "SELECT customer, item, price, quantity, total_price, status FROM orders WHERE order_id = ?",
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

        customer, item_details, price, quantity, total_price, status = order_data

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
                    name="Quantity",
                    value=str(quantity),
                    inline=True,
                )
                log_embed.add_field(
                    name="Unit Price",
                    value=format_price(price),
                    inline=True,
                )
                log_embed.add_field(
                    name="Total Price",
                    value=format_price(total_price),
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
            "SELECT customer, item, price, quantity, total_price, status FROM orders WHERE order_id = ?",
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

        customer, item_details, price, quantity, total_price, old_status = order_data

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
                    name="Unit Price",
                    value=format_price(price),
                    inline=True,
                )
                log_embed.add_field(
                    name="Quantity",
                    value=str(quantity),
                    inline=True,
                )
                log_embed.add_field(
                    name="Total Price",
                    value=format_price(total_price),
                    inline=True,
                )

                await log_channel.send(embed=log_embed)


@bot.event
async def on_ready():
    init_db()
    print(f"Logged in as {bot.user.name} (ID: {bot.user.id})")
    # Approval buttons use custom IDs and are routed in on_interaction,
    # so they continue working after a bot restart without registering
    # one View instance for every approval request.
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

        if custom_id.startswith("approval_check_"):
            if not interaction.user.guild_permissions.manage_messages:
                await interaction.response.send_message(
                    "⚠️ You need **Manage Messages** permission to approve photos.",
                    ephemeral=True,
                )
                return

            approval_id = int(custom_id.split("_")[2])
            await handle_approval_check(interaction, approval_id)
            return

        if custom_id.startswith("approval_reject_"):
            if not interaction.user.guild_permissions.manage_messages:
                await interaction.response.send_message(
                    "⚠️ You need **Manage Messages** permission to reject photos.",
                    ephemeral=True,
                )
                return

            approval_id = int(custom_id.split("_")[2])
            await interaction.response.send_modal(ApprovalRejectModal(approval_id))
            return

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
    quantity="Quantity of the item",
    customer="Name or tag of the customer (Optional)",
    price="The cost of the item (Optional)",
)
async def add_order(
    interaction: discord.Interaction,
    item: str,
    quantity: int,
    customer: str = None,
    price: str = None,
):
    if quantity < 1:
        await interaction.response.send_message(
            "❌ Quantity must be at least 1.", ephemeral=True
        )
        return

    timestamp_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    user_name = str(interaction.user)

    # Handle defaults if parameters are omitted or left blank
    final_customer = customer if customer else "Not Provided"
    final_price = price if price else "0"

    try:
        unit_price_decimal = parse_price(final_price)
        total_price_decimal = calculate_total_price(final_price, quantity)
    except ValueError as e:
        await interaction.response.send_message(f"❌ {e}", ephemeral=True)
        return

    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    cursor.execute(
        """
        INSERT INTO orders
        (user, customer, item, quantity, price, total_price, status, timestamp)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user_name,
            final_customer,
            item,
            quantity,
            str(unit_price_decimal),
            str(total_price_decimal),
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
        name="Unit Price",
        value=format_price(unit_price_decimal),
        inline=True,
    )
    embed.add_field(
        name="Quantity",
        value=str(quantity),
        inline=True,
    )
    embed.add_field(
        name="Total Price",
        value=format_price(total_price_decimal),
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
                name="💰 Unit Price",
                value=format_price(unit_price_decimal),
                inline=True,
            )
            follow_up_embed.add_field(
                name="💵 Total Price",
                value=format_price(total_price_decimal),
                inline=True,
            )
            follow_up_embed.add_field(
                name="🔢 Quantity",
                value=str(quantity),
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
    name="set_approval_channel",
    description="Configure where /approval photo requests will be posted.",
)
@app_commands.describe(channel="The text channel to receive photo approval requests")
@app_commands.checks.has_permissions(administrator=True)
async def set_approval_channel(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    cursor.execute(
        """
        INSERT INTO approval_config (guild_id, approval_channel_id)
        VALUES (?, ?)
        ON CONFLICT(guild_id)
        DO UPDATE SET approval_channel_id = excluded.approval_channel_id
        """,
        (interaction.guild_id, channel.id),
    )

    conn.commit()
    conn.close()

    await interaction.response.send_message(
        f"✅ Photo approval requests will now be posted in {channel.mention}."
    )


@bot.tree.command(
    name="approval",
    description="Submit a photo and customer for approval.",
)
@app_commands.describe(
    photo="The photo that needs approval",
    customer="The customer name or tag",
)
async def approval(
    interaction: discord.Interaction,
    photo: discord.Attachment,
    customer: str,
):
    if not photo.content_type or not photo.content_type.startswith("image/"):
        await interaction.response.send_message(
            "❌ Please upload an image file for the photo.",
            ephemeral=True,
        )
        return

    approval_channel_id = get_approval_channel_id(interaction.guild_id)

    if not approval_channel_id:
        await interaction.response.send_message(
            "❌ The approval channel has not been configured yet. "
            "An administrator needs to use `/set_approval_channel` first.",
            ephemeral=True,
        )
        return

    approval_channel = interaction.guild.get_channel(approval_channel_id)

    if not approval_channel:
        await interaction.response.send_message(
            "❌ The configured approval channel could not be found.",
            ephemeral=True,
        )
        return

    # Read the uploaded photo while the original slash-command attachment
    # is still available, then upload a copy to the approval channel.
    try:
        photo_bytes = await photo.read()
    except (discord.HTTPException, discord.NotFound) as e:
        await interaction.response.send_message(
            f"❌ I could not read the uploaded photo: {e}",
            ephemeral=True,
        )
        return

    filename = os.path.basename(photo.filename) or "approval_photo.png"
    timestamp_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Create the DB row first so the approval ID can be used in button IDs.
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    cursor.execute(
        """
        INSERT INTO approval_requests
        (
            guild_id,
            creator_id,
            creator_name,
            customer,
            photo_filename,
            status,
            timestamp
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            interaction.guild_id,
            interaction.user.id,
            str(interaction.user),
            customer,
            filename,
            "Pending",
            timestamp_str,
        ),
    )

    approval_id = cursor.lastrowid
    conn.commit()
    conn.close()

    embed = discord.Embed(
        title=f"🖼️ Photo Approval #{approval_id}",
        description="A photo is waiting for approval.",
        color=discord.Color.orange(),
        timestamp=datetime.datetime.now(),
    )
    embed.add_field(
        name="👤 Customer",
        value=f"**{customer}**",
        inline=True,
    )
    embed.add_field(
        name="👨‍💻 Submitted By",
        value=interaction.user.mention,
        inline=True,
    )
    embed.add_field(
        name="📊 Status",
        value="**Pending Approval**",
        inline=True,
    )
    embed.set_image(url=f"attachment://{filename}")
    embed.set_footer(text=f"Approval #{approval_id} • {timestamp_str}")

    try:
        approval_file = discord.File(
            fp=io.BytesIO(photo_bytes),
            filename=filename,
        )

        approval_message = await approval_channel.send(
            content="🔔 **New photo requires approval**",
            embed=embed,
            file=approval_file,
            view=ApprovalView(approval_id),
        )

        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE approval_requests
            SET message_id = ?, channel_id = ?
            WHERE approval_id = ?
            """,
            (approval_message.id, approval_channel.id, approval_id),
        )
        conn.commit()
        conn.close()

    except (discord.Forbidden, discord.HTTPException) as e:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM approval_requests WHERE approval_id = ?",
            (approval_id,),
        )
        conn.commit()
        conn.close()

        await interaction.response.send_message(
            f"❌ I could not post the photo in {approval_channel.mention}. "
            f"Please check the bot's permissions.\n`{e}`",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        f"✅ Your photo for **{customer}** was submitted to "
        f"{approval_channel.mention} for approval.",
        ephemeral=True,
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
    quantity="Update the item quantity (Optional)",
)
@app_commands.checks.has_permissions(administrator=True)
async def edit_order(
    interaction: discord.Interaction,
    order_id: int,
    item: str = None,
    customer: str = None,
    price: str = None,
    quantity: int = None,
):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    # Fetch existing data first
    cursor.execute(
        "SELECT item, customer, price, quantity, total_price FROM orders WHERE order_id = ?",
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
    updated_quantity = quantity if quantity is not None else existing[3]
    if updated_quantity < 1:
        await interaction.response.send_message(
            "❌ Quantity must be at least 1.", ephemeral=True
        )
        conn.close()
        return

    try:
        updated_price_decimal = parse_price(updated_price)
        updated_total_price = calculate_total_price(updated_price, updated_quantity)
    except ValueError as e:
        await interaction.response.send_message(f"❌ {e}", ephemeral=True)
        conn.close()
        return

    cursor.execute(
        """
        UPDATE orders
        SET item = ?, customer = ?, price = ?, quantity = ?, total_price = ?
        WHERE order_id = ?
        """,
        (
            updated_item,
            updated_customer,
            str(updated_price_decimal),
            updated_quantity,
            str(updated_total_price),
            order_id,
        ),
    )

    conn.commit()
    conn.close()

    await interaction.response.send_message(
        f"✅ **Order #{order_id} successfully updated!**\n"
        f"• **Customer:** {updated_customer}\n"
        f"• **Item:** {updated_item}\n"
        f"• **Unit Price:** {format_price(updated_price_decimal)}\n"
        f"• **Quantity:** {updated_quantity}\n"
        f"• **Total Price:** {format_price(updated_total_price)}"
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
            quantity AS 'Quantity',
            price AS 'Unit Price',
            total_price AS 'Total Price',
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
