#!/bin/sh

#cp -rv src public
rsync -avh src/ public
for f in $(find public -type f -name '*.html'); do
	sed -i -e '/NAVIGATION/{r partials/navigation.html' -e 'd}' "${f}"
done
