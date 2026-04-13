const express = require('express');
const twilio = require('twilio');
const youtubeDl = require('youtube-dl-exec');
const { v4: uuidv4 } = require('uuid');
const path = require('path');
const fs = require('fs');

const app = express();
app.use(express.urlencoded({ extended: false }));

const accountSid = process.env.TWILIO_ACCOUNT_SID;
const authToken = process.env.TWILIO_AUTH_TOKEN;
const client = twilio(accountSid, authToken);

const PUBLIC_URL = process.env.RENDER_EXTERNAL_URL; // auto-set by Render
const downloadsDir = path.join(__dirname, 'downloads');
if (!fs.existsSync(downloadsDir)) fs.mkdirSync(downloadsDir);

// Serve downloaded files publicly
app.use('/files', express.static(downloadsDir));

app.post('/webhook', async (req, res) => {
  res.sendStatus(200); // Acknowledge Twilio immediately

  const from = req.body.From;
  const songName = req.body.Body?.trim();

  if (!songName) return;

  const to = req.body.To;

  // Tell user we're searching
  await client.messages.create({
    from: to,
    to: from,
    body: `🎵 Searching for "${songName}", please wait...`
  });

  try {
    const fileId = uuidv4();
    const outputPath = path.join(downloadsDir, `${fileId}.mp3`);

    // Download MP3 from YouTube
    await youtubeDl(`ytsearch1:${songName}`, {
      extractAudio: true,
      audioFormat: 'mp3',
      audioQuality: 0,
      output: outputPath,
      noPlaylist: true,
    });

    const fileUrl = `${PUBLIC_URL}/files/${fileId}.mp3`;

    // Send the MP3 back via WhatsApp
    await client.messages.create({
      from: to,
      to: from,
      body: `✅ Here's your song: ${songName}`,
      mediaUrl: [fileUrl]
    });

    // Delete file after 5 minutes to save disk space
    setTimeout(() => {
      if (fs.existsSync(outputPath)) fs.unlinkSync(outputPath);
    }, 5 * 60 * 1000);

  } catch (err) {
    console.error(err);
    await client.messages.create({
      from: to,
      to: from,
      body: `❌ Sorry, I couldn't find or download "${songName}". Try a more specific name.`
    });
  }
});

app.get('/', (req, res) => res.send('Bot is running!'));

const PORT = process.env.PORT || 3000;
app.listen(PORT, () => console.log(`Server running on port ${PORT}`));