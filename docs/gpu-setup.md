# NVIDIA acceleration on Ubuntu

Check the driver with `nvidia-smi`. Then run a local task and check `ollama ps`: the PROCESSOR column reports actual CPU/GPU use. A driver being installed does not by itself mean the model is using it.

If loading the NVIDIA module reports **Key was rejected by service**, Secure Boot may be rejecting its signing certificate. On Ubuntu, inspect the existing certificate before requesting enrollment:

```bash
mokutil --sb-state
mokutil --test-key /var/lib/shim-signed/mok/MOK.der
modinfo -F signer nvidia
openssl x509 -inform DER -in /var/lib/shim-signed/mok/MOK.der -noout -subject
```

When the installed module was signed with that existing, unenrolled key:

```bash
sudo update-secureboot-policy --enroll-key
```

Follow the prompts and create a temporary enrollment password. Save work and reboot normally. In the blue **MOK Manager** screen, choose **Enroll MOK → Continue → Yes**, enter the temporary password, and reboot again. Inspect the certificate when prompted. This step requires your presence at the laptop.

After Ubuntu starts, run `nvidia-smi` again. If needed, run `sudo modprobe nvidia`. If the driver works but Ollama remains on CPU, finish any current task, unload the local model with `ollama stop coding-hub-qwen:8b`, then try another task and check `ollama ps`.

Keep the exact error output if enrollment or driver loading still fails. The dashboard works with CPU inference and cloud agents while the driver issue is resolved.

[Ubuntu Secure Boot documentation](https://documentation.ubuntu.com/security/security-features/platform-protections/secure-boot/)
