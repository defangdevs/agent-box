# Disko layout for a Hetzner Cloud x86 VM.
#
# Hetzner Cloud exposes the root volume as /dev/sda.  The 1 MiB BIOS boot
# partition keeps GRUB usable with GPT on both the stock VM and the rescue
# environment; the remainder is one ext4 root filesystem that grows with the
# server disk.
{ device ? "/dev/sda" }:
{
  disko.devices.disk.main = {
    type = "disk";
    inherit device;
    content = {
      type = "gpt";
      partitions = {
        bios = {
          size = "1M";
          type = "EF02";
        };
        root = {
          size = "100%";
          content = {
            type = "filesystem";
            format = "ext4";
            mountpoint = "/";
          };
        };
      };
    };
  };
}
