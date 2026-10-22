# ODF Pulse Plugin for Claude

Let Claude work with your Pulse dashboards, deals, properties, buyers, broadcasts, and your GoHighLevel sub-account with the ODF Pulse connector.

## Requirements

- An active Open DealFlow Pulse subscription
- Claude with paid plan (Claude API or Claude web with Pro/Team subscription)
- The Claude connector enabled on your Pulse account

## Installation

1. **Add the marketplace** (Customize → Plugins → Add → Add marketplace → Add from a repository)
   - Paste: `https://github.com/JD-OD/odf-pulse-plugin`
   - Click "Add" to sync the marketplace

2. **Install the plugin** (Discover → Odf Pulse → Add)
   - Select the ODF Pulse plugin and add it

3. **Connect your account** (On the plugin page → Connectors → Connect)
   - Sign in to Pulse
   - Select your GoHighLevel sub-account

Once connected, Claude can read your dashboards, properties, deals, and CRM data, and make changes in your CRM. If "Connect" says the connector isn't enabled, contact support@opendealflow.com.

## What Claude can access

- Pulse dashboards and metrics
- Deal pipeline and kanban boards
- Properties and property details
- Buyer information
- Email broadcasts and workflows
- GoHighLevel CRM data for your sub-account

## What Claude can change

Claude can reply to contacts, tag contacts, enroll them in your existing workflows, update records, send SMS and email blasts, and delete records in your GoHighLevel sub-account. Each kind of action (create/update, single send, delete, bulk) is a separate tool, so Claude asks you to approve each kind and you can choose "Allow once" or "Always allow" per tool. Claude is instructed to state the count, exclusions, and finish time and to get your explicit yes before any bulk send or delete. SMS and email blasts only go out between 8am and 8pm in your account's timezone, and email blasts are dripped in batches.

Claude can also read documents (PDFs, spreadsheets, Word files, images) stored in your GoHighLevel account.
