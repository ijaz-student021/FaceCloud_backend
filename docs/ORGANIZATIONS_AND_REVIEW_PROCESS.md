# FaceCloud: Organization aur Photo Review ka process

**Yeh document padhne ke liye hai. Is mein abhi koi code nahi badla gaya.** Pehle process padhein, phir faisle (section 8) bata dein, tab main implement karunga.

Har baat code padh kar likhi gayi hai, aur rejection ka masla ek alag test database par chala kar reproduce kiya gaya hai (section 5).

---

## 1. Organization kya hai (ek minute mein)

| | Personal workspace | Organization (team) |
|---|---|---|
| Kis ka | Har user ko register karte hi **apna akela workspace** milta hai | Ek team, company ya college, jaise "FaceCloud Demo" |
| Log | Sirf wo khud | Owner, moderators, members |
| Photos check kaun karta hai | **Koi nahi.** System ki quality ki raaye sirf "tip" hoti hai, photo accepted maani jati hai | **Moderator** team ki shak wali photos check karta hai |
| Misaal | Aap apni photos upload karte hain | Ek wedding studio: malik = owner, quality checker = moderator, photographers = members |

Sirf ek line yaad rakhein: **organization isliye hai ke team ki photos par koi insaan "haan" ya "nahi" kahe.** Is ke baghair (personal workspace mein) review hota hi nahi.

---

## 2. Kaun kaun hai (roles)

| Role | Kaun hai | Kya kar sakta hai |
|---|---|---|
| **Platform Admin** | FaceCloud ka administrator (aap). Kisi organization ka member nahi hota | Organization banata hai aur uska owner chunta hai; moderator invite karta ya banata hai; kisi bhi photo par faisla kar sakta hai ("approve", "reject", "wapas review mein"); sab kuch dekh sakta hai; contact inbox |
| **Owner** | Organization ka malik (admin chunta hai) | Moderators aur members invite karta hai; unki role badalta hai; organization ka branding aur export; sab kuch dekhta hai. **Seat nahi leta** |
| **Moderator** | Quality checker | Review queue mein photos dekhta hai: approve, reject, hold, escalate. Sirf "normal member" ki role de sakta hai (moderator nahi bana sakta) |
| **Member** | Photographer/uploader | Photos upload karta hai (organization chun kar), apni photos aur faislay dekhta hai |

**Plan ki had (seats):** `free` = 1 moderator + 10 members, `team` = 5 + 100, `enterprise` = unlimited. Jo invitation abhi accept nahi hui wo bhi seat rok leti hai.

**Moderator kya dekh sakta hai (`moderator_data_scope`):** naye organization mein default `own_members` yani har moderator sirf **apne** members ki photos dekhta hai. Owner ya admin isay `organization` kar sakte hain (har moderator poori team dekhe).

---

## 3. Organization banane aur log jodne ka process

```
Admin  --(1)-->  Organization banata hai + owner chunta hai ("Demo Org", owner = admin ya koi user)
Owner/Admin --(2)--> Moderator aur members ko jodta hai (3 tareeqe, neeche)
Member --(3)--> Photo upload karte waqt "Organization" chunta hai (default: apna personal workspace)
Moderator --(4)--> Queue mein photo dekh kar faisla karta hai
```

**Log jodne ke 3 tareeqe aur email ka haal (aaj ka code):**

| Tareeqa | Email jati hai? | Kyun |
|---|---|---|
| **Invite** (naye insaan ki email likhein, role + organization) | **Haan** ("You've been invited", link `/accept-invitation?token=...`, 72 ghante) | Account abhi hai hi nahi, isliye link se banana padta hai |
| **Maujooda user ko moderator banana** (Users screen se role badalna, ya organization mein member ki role badalna) | **Nahi** | Code sirf membership banata hai, uske login tokens reset karta hai aur audit log likhta hai. Koi email ya notification nahi |
| **Admin seedha moderator account banata hai** (password khud deta hai) | **Nahi** | Admin ko khud bataana padta hai |

Aap ne jo pucha tha ("moderator bante hain to unko email kyun nahi jati"): sirf pehle tareeqe par jati hai, doosre do par code mein bheji hi nahi jati. Fix section 7 mein hai.

---

## 4. Photo ka safar (upload se faisle tak)

```
Member photo upload karta hai (Organization chun kar)
        |
        v
Automatic checks: quality (blur, andhera, noise), chehra, duplicate
        |
   +----+-----------------------+-------------------------+
   v                            v                         v
 accepted                  needs_review              rejected (sirf "bohot dhundhli", ya chehra nahi ban raha)
   |                            |                         |
   |            Personal workspace?  --haan-->  accepted maani jati hai (tip milti hai)
   |            Organization?        --haan-->  ModeratorReview ban jata hai (24 ghante ka SLA)
   v                            v
 Enhancement                 Moderator queue (/moderator/queue)
 + person folders                 |
                       Moderator: claim -> approve / reject (wajah likhna zaroori) / hold / escalate
                                  |
                  +---------------+-------------------+
                  v                                   v
          APPROVE: photo accepted ->        REJECT: photo person folders se bahar,
          enhancement chalti hai,           enhancement nahi hoti, member ko wajah milti hai
          folders mein jati hai
                                  |
                  Member ko notification ("photo_review_approved / rejected")
```

**Review ki halatein:** `pending` → `in_review` (kisi ne claim ki) → `approved` / `rejected` / `corrected` / `escalated`. Waqt par faisla na ho to `overdue` ki notification owner/moderator ko jati hai.

**Admin ka faisla:** admin kisi bhi photo par "approve / reject / wapas review mein" kar sakta hai, chahe review ho ya na ho (personal workspace ki photo par bhi). Wo moderator jaise hi effects deta hai aur member ko wajah ke saath batata hai.

**Member ko decision ki email jati hai ya nahi:** decision ki notification **app ke andar** zaroor aati hai. **Email tab hi jati hai jab member ne apni Settings mein "Email notifications" ON kiya ho.** Yeh option **default OFF** hai. Yeh ek wajah hai ke members ko "decision ki email nahi aati". Account wali emails (verification, invitation, password reset) is switch par depend nahi karti, wo hamesha seedhi jati hain.

---

## 5. Rejection ka masla (jo maine reproduce kiya)

Test: ek organization, ek member, ek moderator, ek admin. Member ki do photos "needs_review" mein (ek organization mein, ek personal workspace mein). Phir moderator ne pehli reject ki, admin ne doosri reject ki. Nateeja (member ke **My Images** mein):

| Kya hua | Photo par status ka lafz | **All photos** | Accepted | In review | **Rejected** |
|---|---|:---:|:---:|:---:|:---:|
| Moderator ne organization ki photo reject ki | **"In review"** (galat, reject hona chahiye tha) | **Dikhti hai** | nahi | **Dikhti hai** | dikhti hai |
| Admin ne personal workspace ki photo reject ki | **"Accepted"** (galat) | **Dikhti hai** | **Dikhti hai** | nahi | dikhti hai |
| System ne khud "bohot dhundhli" par reject ki | "Rejected" (sahi) | **Dikhti hai** | nahi | nahi | dikhti hai |

Yani **reject hone ke baad bhi photo "All photos" mein hai**, aur kabhi "In review", kabhi "Accepted" ka lafz bhi dikhata hai. Yehi aap ne dekha.

**Ek aur asar (code se confirm):** enhancement ka rule (`enhancement/eligibility.py`) sirf `ImageUpload.status == "rejected"` ko rokta hai, `quality decision = rejected` ko tab rokta hai jab `QUALITY_CHECKS_REJECT` ON ho (wo OFF hai). Is liye **moderator ki rejected photo ko member haath se "Enhance" bhi kar sakta hai.** Yeh bhi isi masle ka hissa hai.

### Wajah
"Rejected" ko teen jagah alag alag samjha ja raha hai:
1. `ImageUpload.status = "rejected"`: sirf **automatic** "too blurry" par lagta hai.
2. `ImageQualityDecision.decision = "rejected"`: moderator ya admin ka reject **sirf yahan** likhta hai, photo ka apna status nahi badalta.
3. Dikhane ka rule (`image_display_status`) ek setting `QUALITY_CHECKS_REJECT` (default **False**, kyunke product ka kaam enhancement hai) ki wajah se is insani "reject" ko **neeche karke "In review" ya "Accepted" bana deta hai**.

Aur "All photos" tab ka rule hai "sab kuch dikhao", is liye reject hui photo wahan bhi rehti hai.

---

## 6. Jo hona chahiye (maujooda process ke mutabiq tajweez)

**Ek saaf rule:** jab koi insaan (moderator ya admin) photo reject kare, wo **rejected** ho jaye, aur **sirf "Rejected" tab mein** dikhe.

| Cheez | Naya behaviour |
|---|---|
| Reject hone ke baad photo ka status lafz | **"Rejected"** (sab jagah ek jaisa) |
| **All photos** tab | Rejected photos **nahi** dikhengi |
| **Rejected** tab | Saari rejected photos (system ki, moderator ki, admin ki), wajah ke saath |
| Accepted / In review tab | Rejected photo kisi mein nahi aayegi |
| Person folders | Reject par photo folder se nikal jati hai (pehle se hota hai) |
| Enhancement | **Rejected photo enhance nahi hogi**, na automatic, na haath se (abhi haath se ho sakti hai, upar dekhein) |
| Preview, trash, delete | Rejected photo ka preview dikhta hai aur trash/delete ho sakti hai (pehle ke kaam ke mutabiq) |
| Member ko pata chale | Notification mein wajah (pehle jaisa), aur photo ke card par "Rejected: <wajah>" |
| **Moderator ghalati se reject kar de** | "Reopen" ya "approve" karne par photo wapas **All photos** mein aa jati hai aur enhance hoti hai (reject wapas hota hai) |
| Dashboard ginti | "Rejected" ek hi jagah ginti hogi, "All" ki ginti kam hogi |

**Kaise (andar ki baat):** moderator ya admin ke reject par photo ka apna `status` bhi `"rejected"` ho jaye (wajah `rejection_reason` mein), aur approve ya reopen par wapas `"uploaded"`. Is se saare rules ek jagah se chalte hain (dikhana, filter, enhancement band, trash, preview), kyunke pehle se `status="rejected"` ke liye yeh sab bana hua hai.

---

## 7. Implementation ka plan (abhi nahi chalaya)

| # | Kaam | Kahan | Risk |
|---|---|---|---|
| 1 | Moderator/admin reject par `status="rejected"` + `rejection_reason`; approve/reopen par wapas | `moderation/services.py` (`apply_quality_verdict`, jo moderator aur admin dono use karte hain) | Kam: ek hi jagah |
| 2 | "All photos" (aur default list) se rejected nikalna; Rejected tab aur ginti theek | `uploads/views.py` (`MyImageListView`), dashboard counts | Darmiyana: kai screens is list ko use karte hain |
| 3 | Purani rows ka data fix: jin photos ka `quality decision = rejected` ho (insani reject) unka status theek karna | Ek chhota management command (alag se chalega, pehle dry-run) | Kam |
| 4 | Tests: moderator reject, admin reject (personal + org), approve/reopen se wapsi, tab filters, counts | `moderation/`, `uploads/` ke tests | Kam |
| 5 | Moderator/promote par **email + notification** ("Aap ko X organization ka moderator bana diya gaya, login karein") | `accounts/views.py` (role change), `organizations/services.py` (`update_member`), `notifications/` (nayi event type, ek migration). Email fail ho to admin ka kaam na ruke | Kam-darmiyana |
| 6 | Frontend: zyada kuch nahi (list API ka jawab badal jata hai). Sirf rejected card par wajah ka lafz, aur tab ki ginti | `~/Downloads/FYP` | Kam |

Deploy: PR, merge, phir `deploy_vps.sh`. Andaza **1 se 1.5 ghanta**. Kal defence hai, is liye main tab tak kuch live nahi karunga jab tak aap na kahein.

---

## 8. Faisle jo aap ko karne hain (jawab dene se main shuru kar dunga)

1. **"All photos" mein rejected photos nahi dikhni chahiye**, yeh pakka? (Mera khayal: haan, aur sirf "Rejected" tab mein.)
2. **System ki khud ki "bohot dhundhli" reject** bhi All se hatayen? (Mera mashwara: haan, sab jagah ek jaisa rule.)
3. Moderator ka reject **wapas ho sakta hai** (approve/reopen se photo wapas All mein aaye)? (Mera mashwara: haan.)
4. **Moderator/promote par email** (kaam 5) abhi karein ya defence ke baad?
5. Member ke liye **"Email notifications" default ON** karein? (Abhi OFF hai, is liye decision ki email nahi aati. ON karne se unhein email milti rahegi, lekin purani wali ki setting mein badlaav nahi aayega, sirf naye accounts par.)

---

## 9. Defence ke liye demo script (5 minute, abhi ke code ke mutabiq chal sakti hai)

1. **Admin** login: Organizations screen se organization "FaceCloud Demo" banayein, owner chunein.
2. Admin se ek **moderator ko invite** karein (email likh kar, role moderator, organization chunein). Invitation ki email aaye, link se account banayein.
3. Dusra account **member** (ya moderator se invite karein). Member login kare.
4. **Member** photo upload kare aur upload ke waqt **organization = "FaceCloud Demo"** chune (personal workspace nahi).
5. Aisi photo upload karein jo andheri ya dhundhli ho, taake `needs_review` aaye.
6. **Moderator** login: Queue (`/moderator/queue`) mein photo dikhe, claim, **Reject** (wajah likhein) ya Approve.
7. **Member** dobara dekhe: notification, photo ka status, aur (fix ke baad) Rejected tab mein photo.
8. **Admin** ka Inbox, audit log, aur Organization ki dashboard figures (kitne review, kitne rejected, SLA) dikhayein.

Yaad rakhein: **personal workspace mein review nahi hota.** Agar demo mein member apna photo personal workspace mein upload kar de to moderator ki queue mein kuch nahi aayega.
