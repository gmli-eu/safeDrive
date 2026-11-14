# Junction 2026 challenges

The challenges safeDrive is entered in at Junction 2026 ("European Tech Renaissance"), copied from the challenge pages on the Junction hacker dashboard. Keep this file up to date when a challenge page changes.

**Final submission deadline:** Sunday 2026-11-15 at 10:00 Finnish time (EET, UTC+2).

## Summary

| Challenge | Sponsor | Prize | Must have |
|---|---|---|---|
| [Atech Real World Challenge](#atech-real-world-challenge) | Atech | Pro Atech kit; runner-ups get kits and swag | Atech modules in the live demo; public repo or Atech export; ~2-min video or live demo; summary posted in Atech Discord |
| [Best use of Arduino UNO Q](#best-use-of-arduino-uno-q) | Arduino | UNO Q + Plug and Make kit per member | Project published on Arduino Project Hub with tag **Junction Finland 2026**, as well as on Junction |
| [Best Use of Nemotron](#best-use-of-nemotron) | NVIDIA | RTX 5080 (2 winning teams) | Nemotron 3.5 Super VL in the working solution; text in the submission form explaining it |
| [Best Use of Gemma](#best-use-of-gemma) | Google DeepMind | €1,000 each, top 3 teams | None stated |
| [Best Use of Gemini API](#best-use-of-gemini-api) | Google DeepMind | €4,000 + $1,000 GCP credits per member / €2,000 / €1,000 | Explain the problem, the integration, what is innovative, and why Gemini matters |
| [Verda Compute Track](#verda-compute-track) | Verda | $10,000 credits + hoodies / $2,500 / $2,500 | Process documented; summary link posted in Verda Discord |

## Atech Real World Challenge

**By Atech. "The API for the physical world."**

Build a physical device that solves a real problem for a real person (a farmer, a nurse, a cyclist, your grandmother...), with Atech modules as its body and AI as its brain. It should sense, react or connect in a way software alone can't. Atech hardware is required; how it combines with other tools, APIs and models is up to the team.

**Minimum requirements**
- Atech modules used in the demo, as part of a real-world solution.
- A link to a summary or post about it in the Atech Discord.

**Submission requirements**
- Live demo on the hardware.
- Public GitHub repo or exported Atech project.
- About a 2-minute video or a live demo.

**Judging (25% each)**
- **Real-world validation of usefulness:** evidence it's genuinely useful (tested with real users, experts or real-world data).
- **Real-world impact:** a real user and a real problem, solved convincingly.
- **Use of Atech:** how well and how creatively the hardware and AI platform were used.
- **Working demo & storytelling:** runs live on the hardware in front of the judges, with a clear story and pitch.
- **Bonus (0%):** extra credit for combining several modules, or connecting the device to AI models and external APIs.

**Prizes:** the winner gets a special Pro Atech kit. Runner-ups get Atech kits and swag. Top teams are invited to show their project on Atech's channels and visit Atech in Copenhagen.

**Technology:** snap-together ESP32 modules (distance, IMU, temperature & humidity, air quality, microphone, speakers, motors...) and an AI platform at atech.dev that writes, builds and flashes firmware from the browser (Chrome or Edge, WebSerial). Generated firmware is C/C++ (PlatformIO / ESP32) and can be edited by hand. Python library: `pip install atech`. There's an emulator, and devices connect over Wi-Fi or Bluetooth. Platform code: `JUNCTION`. Kits are returned after the final demos.

**Inspiration from the page:** assistive devices (fall detection, posture coaching, reminders for people living alone), sustainability sensing, physical AI (LLM or vision model in a loop with sensors and actuators), robotics, maker tools.

**Support:** #atech on the Junction Discord, the Atech booth (night shift too). Docs: atech.dev/docs. Examples: github.com/atechdotdev.

## Best use of Arduino UNO Q

**By Arduino. "Bring your idea to life in the physical world with Edge AI and the Arduino UNO Q."**

Use the Arduino UNO Q and Modulino sensors in the project, for any challenge. Run AI models on the device, sense the real world, make things move, light up or react. The best projects combine a working physical prototype with smart use of on-device AI.

**Submission (required for prizes):** publish on the Arduino Project Hub as well as on the Junction platform.
1. Log in at https://projecthub.arduino.cc/
2. Click **+ NEW PROJECT**.
3. Select the tag **Junction Finland 2026**.
4. Fill in the required parts.

**Judging**
- Use of Arduino hardware & Edge AI: 50%
- Working prototype & demo: 30%
- Creativity: 20%

**Prize:** each member of the winning team gets an Arduino UNO Q and an Arduino Plug and Make Kit.

**Hardware lent at the Arduino booth / Hardware Lab:** UNO Q (2 GB RAM, 16 GB eMMC), USB-C cable, USB-C hub (8 in 1), 45 W USB-C power supply, Arduino USB camera, Modulino Buzzer, Knob and Thermo. Software: Arduino App Lab. Booking: https://2026.hackjunction.com/hardware

Arduino also runs the main challenge "The Green European Renaissance" (Edge AI on the UNO Q for a greener Europe). This tech challenge is open to every team.

## Best Use of Nemotron

**By NVIDIA. Applies to any track.**

Build a project where NVIDIA Nemotron does real work through reasoning, tool use or coordinated workflows, for a clear audience. It can be used alone or with other models, tools and data. Nemotron must contribute directly to the core functionality, and the demo must make that contribution visible.

**Minimum requirements**
- Use **NVIDIA Nemotron 3.5 Super VL** in the working solution.
- Show its contribution to the core functionality.
- Write a short explanation **as text in the submission form**: which model, what it does, why it matters, and how you improved its outputs.

**Judging (25% each)**
- **Meaningful Nemotron usage:** central to the project's value; the team clearly explains what it does and why it matters.
- **Technical execution:** the demo works reliably; thoughtful choices around architecture, data flow, tool use, latency or error handling.
- **Quality of AI output:** useful, relevant, grounded where appropriate; evidence of improving quality through prompting, evaluation or feedback loops.
- **Creativity and differentiation:** thoughtful or inventive use, a distinct approach.

**Prize:** an NVIDIA RTX 5080 for each of the two best teams.

**Model:** Nemotron 3.5 Super VL, 120B parameters with 12B active per token; reads text, images, documents and video.

**Access:** NVIDIA Build (OpenAI-compatible, base URL `https://integrate.api.nvidia.com/v1`, free tier about 40 requests/min per key, so use retries with backoff and one key per teammate); OpenRouter (`:free` variants: 20 req/min, 50/day); Hugging Face weights; the Verda-hosted endpoint.

**Support:** NVIDIA Discord channel and the NVIDIA table. Judges: NVIDIA team.

## Best Use of Gemma

**By Google DeepMind.**

For the most innovative and impactful use of Google's Gemma open models: creative applications, strong technical execution, and solutions that show what open models can do for real-world problems. No minimum requirements or judging weights are listed.

**Prize:** €1,000 per team for the top 3 teams, unranked.

**Access:** Featherless AI (hosted API), Modal (serverless GPU), Verda (cloud GPU), Google AI Studio, Hugging Face. See the Gemma docs and the Junction Hackerpack for credits.

## Best Use of Gemini API

**By Google DeepMind. Open innovation, any industry.**

Show a meaningful, innovative and technically impressive use of Gemini that goes beyond "adding an LLM to an app". The submission must clearly explain:
- what problem you're solving and why it matters,
- how you integrated the Gemini API,
- what makes your use of Gemini innovative or technically impressive,
- why Gemini is an important part of your solution.

**Prizes:** 1st: €4,000 plus $1,000 Google Cloud credits per team member. 2nd: €2,000. 3rd: €1,000.

**Notes:** credits for up to 400 teams via a form on the challenge page; participants must be 18 or older. Keys from Google AI Studio. Support at the Google DeepMind booth.

## Verda Compute Track

**By Verda. "Best use of Verda."**

Pick any Junction challenge and use Verda products to build something you couldn't build on a laptop. Ideas on the page: train robot locomotion or imitation policies, RL rollouts and fine-tuning, embed all of Wikipedia for RAG, **keep sensitive data in Europe by running open-weight models on infrastructure you control**. No crypto mining.

**Minimum requirements**
- Document your process.
- Share a link to a summary in the Verda Discord channel.

**What they look for:** an ambitious idea where compute makes it possible; the right Verda product with a clear reason why; the path from first idea to working prototype; bonus for showing how it scales from demo to real users.

**Judging (25% each)**
- **Ambition:** how much did compute let you do that you otherwise couldn't?
- **Fit:** did you pick the right Verda services and use them well?
- **Working prototype:** does it run, and is the process documented?
- **Path to scale:** is there a credible way from demo to real users?

**Prizes:** 1st: $10,000 Verda cloud credits and hoodies. 2nd and 3rd: $2,500 cloud credits each.

**Products:** Inference API (open-weight models, including the NVIDIA Nemotron endpoint hosted with NVIDIA), GPU instances and Instant Clusters, CPU instances, auto-scaling containers, batch jobs. Free credits: QR code on the table or verda.com/blog/junction-2026. Mentors at Booth 6, third floor.
