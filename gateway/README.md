# Running the class gateway

Each class needs one **gateway VM**: the VM that runs Bifrost and hands out
student keys. Do everything below on that VM, as the `student` user.

## 0. Set up the gateway (once per class)

Get this repo onto the gateway VM and run the setup script:

```
git clone https://github.com/csfeeser/bifrost-class-gateway.git
python3 bifrost-class-gateway/gateway/setup-gateway.py
```

(Already cloned from an earlier class? Run `git -C bifrost-class-gateway pull`
first to get the latest version.)

If it asks for the Anthropic API key, paste it and press Enter (you won't see
it as you paste). It ends with **Gateway ready.** and prints three addresses:

- **Students' gateway URL** -- already built into the student key blocks; you don't need to copy it.
- **Bifrost admin UI** -- for checking usage or turning off a key.
- **Student key page** -- you'll use this in step 2.

Running it again is safe: it keeps the existing logins and keys.

## 1. Make the student keys

```
python3 ~/bifrost/bin/make-student-keys.py 12
```

Replace `12` with the number of students. It prints one line per student and
ends with `Wrote 12 students ...`. Running it again is safe: existing keys are
reused, and a bigger number adds more students.

## 2. Open the key page

Open the **Student key page** address that step 0 printed (this VM's aux2
address). Log in with the username and password from:

```
cat ~/bifrost/keysite.env
```

## 3. Give each student VM its key

For each student VM: click the next block that **isn't** grayed out (it copies
itself and turns gray), paste it into a terminal on that student's VM, and
press Enter. You should see `student-NN key saved`.

Clicked the wrong one? Press **Undo** on that block.

---

### If something's wrong

- **Page doesn't load, or students get "could not reach the gateway":** run
  `python3 bifrost-class-gateway/gateway/setup-gateway.py` again. It restarts anything that's
  stopped and checks everything.
- **Page is empty:** step 1 hasn't been run yet on this VM.
- **Never** copy `~/bifrost/.env` to a student VM. It holds the Anthropic key
  and the admin password. Students only ever get the blocks from the key page.
