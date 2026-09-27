(function () {
    function initCarouselControls() {
        const headers = document.querySelectorAll('.carousel-header');

        headers.forEach((header) => {
            const buttons = header.querySelectorAll('.carousel-scroll-button');
            if (buttons.length < 2) return; // Need both left and right buttons

            const [leftBtn, rightBtn] = buttons;

            // Find the next carousel element after this header
            let carousel = header.nextElementSibling;
            while (carousel && !carousel.classList.contains('title-carousel')
                          && !carousel.classList.contains('continue-watching-carousel')) {
                carousel = carousel.nextElementSibling;
            }

            if (!carousel) return; // No carousel found, nothing to wire up

            const scrollByPage = (direction) => {
                carousel.scrollBy({
                    left: direction * carousel.clientWidth,
                    behavior: 'smooth'
                });
            };

            leftBtn.addEventListener('click', () => scrollByPage(-1));
            rightBtn.addEventListener('click', () => scrollByPage(1));

            const updateButtonState = () => {
                const maxScroll = carousel.scrollWidth - carousel.clientWidth;
                const atStart = carousel.scrollLeft <= 1;
                const atEnd = carousel.scrollLeft >= maxScroll - 1;

                leftBtn.disabled = atStart;
                rightBtn.disabled = atEnd || maxScroll <= 0;
            };

            carousel.addEventListener('scroll', updateButtonState);
            window.addEventListener('resize', updateButtonState);

            // scrollWidth can be wrong until images finish loading
            carousel.querySelectorAll('img').forEach((img) => {
                if (!img.complete) {
                    img.addEventListener('load', updateButtonState, { once: true });
                }
            });

            updateButtonState();
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initCarouselControls);
    } else {
        initCarouselControls();
    }
})();
