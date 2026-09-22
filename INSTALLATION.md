# Marstek Battery System - Installation Guide

This guide walks you through the complete installation and setup process for the Marstek Battery System integration.

## Table of Contents

1. [Prerequisites](#prerequisites)
2. [Preparing Your Marstek Device](#preparing-your-marstek-device)
3. [Installation Methods](#installation-methods)
4. [Configuration](#configuration)
5. [Verification](#verification)
6. [Next Steps](#next-steps)

## Prerequisites

Before you begin, ensure you have:

- ✅ Home Assistant installed and running (minimum version 2023.1.0 or higher recommended)
- ✅ Marstek battery system (Venus A, C, D, E, or E mini)
- ✅ Device connected to your local network via WiFi or Ethernet
- ✅ Marstek mobile app installed on your phone
- ✅ Access to your Home Assistant configuration files (for manual installation)
- ✅ Access to your router/network settings (recommended for static IP)

## Preparing Your Marstek Device

### Step 1: Connect Device to Network

1. **Power on your Marstek device**
   - Connect to power source
   - Wait for device to boot (status lights should indicate ready state)

2. **Connect via Marstek App**
   - Open the Marstek mobile application
   - Add/connect your device following the app instructions
   - Configure WiFi or ensure Ethernet connection is active

3. **Verify Network Connection**
   - In the app, navigate to device settings
   - Note the device's IP address (you'll need this later)
   - Ensure the device shows as online

### Step 2: Enable Open API

1. **In the Marstek App:**
   - Go to Settings → Device Settings
   - Find "Open API" or "Local API" option
   - Toggle it ON

2. **Configure UDP Port:**
   - Default port: 30000
   - Recommended: Use a port between 49152-65535
   - Note: Higher ports reduce conflicts with other services
   - Save your settings

3. **Important Notes:**
   - Enabling Open API may disable some built-in device features to prevent command conflicts
   - The device must remain connected to your network for the integration to work
   - Open API is only accessible from the local network (not over internet)

### Step 3: Set Static IP (Highly Recommended)

Setting a static IP prevents connection issues if your router reassigns IP addresses.

**Option A: Set in Router**
1. Log into your router admin panel
2. Find DHCP reservations or static IP settings
3. Find your Marstek device (by MAC address or hostname)
4. Assign a static IP address
5. Save and restart router if required

**Option B: Set in Device (if supported)**
Some Marstek models may allow static IP configuration in the mobile app.

**Recommended IP Range:** Use an IP outside your router's DHCP range (e.g., 192.168.1.200)

## Installation Methods

Choose one of the following installation methods:

### Method 1: HACS (Recommended)

[HACS](https://hacs.xyz/) makes installation and updates easy.

1. **Install HACS** (if not already installed)
   - Follow instructions at https://hacs.xyz/docs/setup/download

2. **Add Custom Repository**
   - Open Home Assistant
   - Go to HACS → Integrations
   - Click three dots (⋮) → Custom repositories
   - Add repository URL: `https://github.com/yourusername/ha-marstek`
   - Category: Integration
   - Click ADD

3. **Install Integration**
   - Search for "Marstek Battery System"
   - Click on the integration
   - Click DOWNLOAD
   - Select latest version
   - Click DOWNLOAD again

4. **Restart Home Assistant**
   - Settings → System → Restart

### Method 2: Manual Installation

1. **Download the Integration**
   - Download the latest release from GitHub
   - Or clone the repository: `git clone https://github.com/yourusername/ha-marstek.git`

2. **Copy Files**
   ```bash
   # Navigate to your Home Assistant config directory
   cd /config
   
   # Create custom_components directory if it doesn't exist
   mkdir -p custom_components
   
   # Copy the marstek folder
   cp -r /path/to/downloaded/marstek custom_components/
   ```

3. **Verify File Structure**
   ```
   config/
   └── custom_components/
       └── marstek/
           ├── __init__.py
           ├── binary_sensor.py
           ├── config_flow.py
           ├── const.py
           ├── manifest.json
           ├── marstek_api.py
           ├── number.py
           ├── select.py
           ├── sensor.py
           └── strings.json
   ```

4. **Restart Home Assistant**
   - Settings → System → Restart
   - Or via command line: `ha core restart`

## Configuration

### Adding the Integration

1. **Navigate to Integrations**
   - Go to Settings → Devices & Services
   - Click "+ ADD INTEGRATION" button (bottom right)

2. **Search for Marstek**
   - Type "Marstek" in the search box
   - Click on "Marstek Battery System"

3. **Enter Connection Details**
   - **IP Address**: Enter your device's IP address (e.g., 192.168.1.100)
   - **UDP Port**: Enter the port you configured (default: 30000)
   - Click SUBMIT

4. **Wait for Discovery**
   - The integration will attempt to connect to your device
   - This may take a few seconds

5. **Success!**
   - You should see "Success! The Marstek device has been added."
   - Your device will appear in the Devices & Services list

### Troubleshooting Setup

**"Cannot Connect" Error:**
- Verify IP address is correct
- Ensure Open API is enabled in Marstek app
- Check that device and HA are on same network
- Try pinging the device: `ping 192.168.1.100`
- Verify UDP port is correct
- Check firewall isn't blocking UDP traffic

**"Device Already Configured" Error:**
- The device is already added
- Check Settings → Devices & Services for existing entry
- Remove old entry if you want to reconfigure

## Verification

### Check Device Status

1. **View Device**
   - Settings → Devices & Services → Marstek Battery System
   - Click on your device name
   - You should see device information and all entities

2. **Verify Entities**
   - Check that sensors are showing data
   - Battery SOC should show a percentage
   - Power values should be present (may be 0 if no activity)

3. **Test Controls**
   - Try changing the operating mode
   - If in Passive mode, adjust the power setting
   - Changes should apply within a few seconds

### Enable Logging (Optional)

Add to `configuration.yaml` to see debug information:

```yaml
logger:
  default: warning
  logs:
    custom_components.marstek: debug
```

Check logs: Settings → System → Logs

## Next Steps

### Dashboard Configuration

1. **Create Energy Dashboard Card**
   ```yaml
   type: entities
   title: Marstek Battery System
   entities:
     - sensor.marstek_battery_state_of_charge
     - sensor.marstek_battery_power
     - sensor.marstek_grid_power
     - sensor.marstek_solar_power  # Venus A and Venus D only
     - select.marstek_operating_mode
   ```

2. **Add to Energy Dashboard**
   - Settings → Dashboards → Energy
   - Configure grid, solar, and battery sensors

### Create Automations

See README.md for automation examples like:
- Charging during off-peak hours
- Discharging during peak rates
- Mode switching based on time/conditions

### Monitor Performance

- Check entity states regularly
- Review logs for any errors
- Monitor network connectivity
- Keep firmware updated via Marstek app

## Common Issues and Solutions

### Issue: Entities Not Updating

**Solutions:**
1. Check device is online in Marstek app
2. Verify network connection
3. Reload integration: Settings → Devices & Services → Marstek → ⋮ → Reload
4. Check logs for errors
5. Restart Home Assistant

### Issue: Connection Drops

**Solutions:**
1. Set static IP address
2. Check WiFi signal strength
3. Reduce scan interval (see advanced configuration)
4. Check router logs for network issues

### Issue: Wrong Values

**Solutions:**
1. Verify API is enabled in Marstek app
2. Check device firmware is up to date
3. Compare values with Marstek app
4. Report issue if values consistently differ

## Advanced Configuration

### Custom Scan Interval

Edit `__init__.py` to change update frequency:

```python
SCAN_INTERVAL = timedelta(seconds=30)  # Default is 30 seconds
```

Lower values = more frequent updates but higher network usage.

### Network Optimization

If you experience issues on busy networks:

1. Use wired Ethernet connection instead of WiFi
2. Assign static IP with DHCP reservation
3. Use Quality of Service (QoS) rules in router
4. Ensure device is on 2.4GHz WiFi (better range than 5GHz)

## Getting Help

If you encounter issues:

1. **Check Documentation**
   - Review README.md
   - Check troubleshooting sections

2. **Search Existing Issues**
   - GitHub: https://github.com/yourusername/ha-marstek/issues

3. **Community Support**
   - Home Assistant Community Forums
   - Home Assistant Discord

4. **Report Bugs**
   - Include Home Assistant version
   - Include integration version  
   - Include relevant logs
   - Describe steps to reproduce

## Updating the Integration

### Via HACS
1. HACS → Integrations
2. Find "Marstek Battery System"
3. Click UPDATE if available
4. Restart Home Assistant

### Manual Update
1. Download latest release
2. Replace files in `custom_components/marstek/`
3. Restart Home Assistant

---

**Installation Complete!** You now have full local control of your Marstek battery system through Home Assistant.
