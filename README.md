# Job Scout

Job Scout is a private Windows web app for scanning company career feeds, saving openings, and tracking referral conversations.

## Start

Open **Launch Job Scout.cmd**. It installs the Python dependencies on the first run and opens [Job Scout on this computer](http://127.0.0.1:8765/). Keep the server window open while using the app. Press Ctrl+C there to stop it. If the browser stays closed, open the link above. The launcher uses uv when installed and otherwise needs Python 3.11 or newer.

The SQLite file is stored at **%LOCALAPPDATA%\JobScout\scout.sqlite3**. Close the app before copying it for backup. The source folder can be moved without moving your database.

## Find jobs and ask for referrals

1. Click **Find latest jobs**, or type it in **Commands** (Ctrl+K). The watchlist starts with 200 companies. It includes 164 automated sources across Greenhouse, Lever, Ashby, and SmartRecruiters. The other 36 have careers-page links and a **Manual source** status. Scan coverage reflects the current run.
2. Use the location filter or search by role or company. The initial scan establishes a baseline. Later scans identify listings first observed on a previously checked board. **First seen** is the date Job Scout found the role. **Posted** is the employer’s date when supplied.
3. Review a role, check the quoted location evidence, and open the direct application link. Location and experience tags are estimates from the listing. Verify them before applying. You can also add a job URL manually.
4. Use **Find people** or **Add someone I know**. Review a suggested person’s source and confirm their current affiliation before sending. Open LinkedIn, Gmail, or your community to send the draft yourself, then select **Record as sent**. Mark the application separately from the conversation.
5. Reopen **Referrals** to check due conversations, record replies, and distinguish a referral offer from confirmation it was submitted.

The source registry and status checks took inspiration from [job-alerts](https://github.com/SirFuzzyWalls/job-alerts). The application reads documented employer feeds directly. [Greenhouse](https://docs.greenhouse.io/job-board.html) · [Lever](https://github.com/lever/postings-api) · [Ashby](https://developers.ashbyhq.com/docs/public-job-posting-api) · [SmartRecruiters](https://developers.smartrecruiters.com/docs/endpoints)

## Optional integrations

Configure Brave and Google in **Settings**. Job Scout stores those credentials in Windows Credential Manager. The environment variables in **.env.example** are also supported and require a restart when edited.

- **Public contact research.** Save a [Brave Search API key](https://api-dashboard.search.brave.com/app/documentation/web-search/get-started) in Settings. Searches use the company and job specialty. Without a key, the app returns saved contacts and a LinkedIn search query.
- **Gmail.** In your Google Cloud project, enable Gmail API, configure the consent screen and your test user if applicable, and create a **Desktop app** OAuth client. Open Job Scout at **127.0.0.1:8765**, choose the downloaded JSON in Settings, then click **Connect Gmail**. The app requests the Gmail metadata scope. Google may require you to reconnect while using a consent screen in testing mode. [Google OAuth setup](https://developers.google.com/identity/protocols/oauth2/native-app)
- **Link Gmail conversations.** In Gmail, label a sent, single-recipient thread **Job Scout**. Record that send in the app with Gmail as its channel, then select **Link Gmail thread** and choose the labeled conversation. Job Scout reads the linked thread’s metadata and looks for replies from its recipient. Checks run every ten minutes while the app is open and on demand. Automated messages and delivery failures can require manual review.
- **LinkedIn email hints.** Optionally enable message notification emails in LinkedIn. Create a Gmail filter that applies **Job Scout LinkedIn** to those notification emails, then turn on hints in Job Scout Settings. Hints are reminders to check LinkedIn and never confirm a reply or change a conversation by themselves.
- **Draft rewrite.** Set **SCOUT_OPENAI_KEY** and **SCOUT_OPENAI_MODEL** to enable **AI rewrite**. Clicking it sends the draft text to the configured API and may incur model charges. Job scans, templates, and reply checks run without model calls.

## Developer checks

From this folder, run **python -m pytest -q** with the runtime requirements and pytest installed. Set **SCOUT_DB** to a different SQLite path to use an isolated workspace.
