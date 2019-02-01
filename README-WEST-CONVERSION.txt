This Zephyr microPlatform installation was converted from repo to a
west-based installation using repo sync.

To keep using it, please install west with pip:

# Linux
pip install --user west

# macOS, Windows
pip install west

From this point on, use 'git pull' in the manifest directory, followed
by 'west update', to fetch updates. (Your manifest repository should
be tracking an upstream branch.) For more information on west, see:

https://docs.zephyrproject.org/latest/tools/west/index.html

These older directories (and their subdirectories) are no longer
needed, and can be removed if you don't need to use older versions of
the Zephyr microPlatform:

- .repo
- zmp-build
- zephyr-fota-samples

You can also remove the zmp symlink in the top of the ZmP installation.

Feel free to keep them around if you want, though. They won't mess
anything up.
