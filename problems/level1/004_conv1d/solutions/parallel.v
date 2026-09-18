// Sanity-check submission: output-bound 1-D convolution.
// Buffers x and w, then presents one output word per cycle, computing the K
// taps combinationally for the current (f, o). Indices advance only on
// acceptance, so the word stays stable while stalled. Repeated transactions
// reset the capture state when the last output word is accepted.

module dut #(
    parameter IN_LEN = 128,
    parameter F      = 4,
    parameter K      = 4,
    parameter LANES  = 16,
    parameter DATA_W = 8,
    parameter ACC_W  = 32
)(
    input  wire                    clk,
    input  wire                    rst_n,

    input  wire [LANES*DATA_W-1:0] in_x_flat,
    input  wire                    in_x_flat_valid,
    output wire                    in_x_flat_ready,

    input  wire [LANES*DATA_W-1:0] in_w_flat,
    input  wire                    in_w_flat_valid,
    output wire                    in_w_flat_ready,

    output wire                    out_valid,
    input  wire                    out_ready,
    output wire signed [ACC_W-1:0] out_c
);

    localparam OUT_POS = IN_LEN - K + 1;
    localparam X_BEATS = IN_LEN / LANES;
    localparam W_ELEMS = F * K;
    localparam W_BEATS = (W_ELEMS + LANES - 1) / LANES;

    localparam S_RECV = 1'b0, S_OUT = 1'b1;

    reg        state;
    integer    x_beat;
    integer    w_beat;
    integer    idx;
    reg        x_done;
    reg        w_done;
    integer    f;
    integer    o;
    reg [DATA_W-1:0] x_mem [0:IN_LEN-1];
    reg [DATA_W-1:0] w_mem [0:W_ELEMS-1];

    assign in_x_flat_ready = (state == S_RECV) && !x_done;
    assign in_w_flat_ready = (state == S_RECV) && !w_done;
    assign out_valid       = (state == S_OUT);
    assign out_c           = sum;

    reg signed [ACC_W-1:0] sum;
    integer k;
    always @(*) begin
        sum = {ACC_W{1'b0}};
        for (k = 0; k < K; k = k + 1) begin
            sum = sum
                + $signed(x_mem[o + k])
                * $signed(w_mem[f*K + k]);
        end
    end

    always @(posedge clk) begin
        if (!rst_n) begin
            state  <= S_RECV;
            x_beat <= 0;
            w_beat <= 0;
            x_done <= 1'b0;
            w_done <= 1'b0;
            f      <= 0;
            o      <= 0;
        end else begin
            case (state)
                S_RECV: begin
                    if (!x_done && in_x_flat_valid) begin
                        for (idx = 0; idx < LANES; idx = idx + 1)
                            x_mem[x_beat*LANES + idx] <= in_x_flat[idx*DATA_W +: DATA_W];
                        if (x_beat == X_BEATS - 1) begin
                            x_beat <= 0;
                            x_done <= 1'b1;
                        end else begin
                            x_beat <= x_beat + 1;
                        end
                    end
                    if (!w_done && in_w_flat_valid) begin
                        for (idx = 0; idx < LANES; idx = idx + 1)
                            if (w_beat*LANES + idx < W_ELEMS)
                                w_mem[w_beat*LANES + idx] <= in_w_flat[idx*DATA_W +: DATA_W];
                        if (w_beat == W_BEATS - 1) begin
                            w_beat <= 0;
                            w_done <= 1'b1;
                        end else begin
                            w_beat <= w_beat + 1;
                        end
                    end
                    if (x_done && w_done) begin
                        f     <= 0;
                        o     <= 0;
                        state <= S_OUT;
                    end
                end

                S_OUT: begin
                    if (out_ready) begin
                        if (o == OUT_POS - 1) begin
                            o <= 0;
                            if (f == F - 1) begin
                                f      <= 0;
                                x_done <= 1'b0;
                                w_done <= 1'b0;
                                state  <= S_RECV;
                            end else begin
                                f <= f + 1;
                            end
                        end else begin
                            o <= o + 1;
                        end
                    end
                end

                default: state <= S_RECV;
            endcase
        end
    end

endmodule
